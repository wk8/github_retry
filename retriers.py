import base64
import contextlib
import os
import pathlib
import subprocess

from config import Config
from models import PullRequest


class BaseRetrier(object):
    def cleanup(self, pr_processor):
        raise NotImplementedError

    def retry(self, pr_processor, pr_checks_status):
        raise NotImplementedError

    @staticmethod
    def _github_pr(pr_processor):
        pr = pr_processor.pull_request
        return pr_processor.client.get_repo(pr.repo).get_pull(pr.number)


class GitAmendPushRetrier(BaseRetrier):
    def cleanup(self, pr_processor):
        # nothing to do here
        pass

    def retry(self, pr_processor, _pr_checks_status):
        gh_pr = self.__class__._github_pr(pr_processor)
        pr_repo = gh_pr.head.repo.full_name

        git_env = self.__class__._git_env(pr_processor, pr_repo)

        self.__class__._clone_repo_if_needed(pr_repo, git_env)

        self.__class__._git_command(pr_repo, 'clean -fdx')
        self.__class__._git_command(pr_repo, 'fetch origin', env=git_env)
        branch = gh_pr.head.ref
        self.__class__._git_command(pr_repo, 'checkout %s' % (branch, ))
        self.__class__._git_command(pr_repo, 'reset --hard origin/%s' % (branch, ))
        self.__class__._git_command(pr_repo, 'commit --amend --no-edit')

        new_sha = self.__class__._git_command(pr_repo, 'rev-parse HEAD')
        if not PullRequest.is_valid_sha(new_sha):
            raise RuntimeError('New sha is invalid: %s' % (new_sha, ))
        pr_processor.pull_request.last_processed_sha = new_sha

        self.__class__._git_command(pr_repo, 'push --force', env=git_env)

    _WORKING_DIR = os.path.join(Config.HOME_DIR, 'amend_push_retrier')
    _REPOS_ROOT = os.path.join(_WORKING_DIR, 'repos')
    _SSH_KEYS = os.path.join(_WORKING_DIR, 'ssh_keys')

    # no need to get a full blown git lib for what we do here
    # subpath is relative to the repos' root
    @classmethod
    def _git_command(cls, subpath, subcommand, **kwargs):
        with cls._with_chdir(os.path.join(cls._REPOS_ROOT, subpath)):
            output = subprocess.check_output('git ' + subcommand,  shell=True, stderr=subprocess.STDOUT, **kwargs)
            if isinstance(output, bytes):
                output = output.decode()
            return output.strip()

    @classmethod
    @contextlib.contextmanager
    def _with_chdir(cls, path):
        pwd = os.getcwd()
        try:
            cls._mkdir_chdir(path)
            yield
        finally:
            os.chdir(pwd)

    @staticmethod
    def _mkdir_chdir(path):
        try:
            os.chdir(path)
        except FileNotFoundError:
            pathlib.Path(path).mkdir(parents=True, exist_ok=True)
            os.chdir(path)

    @classmethod
    def _git_env(cls, pr_processor, pr_repo):
        key_path = cls._render_ssh_key(pr_processor, pr_repo)
        # see https://stackoverflow.com/a/28527476
        # requires git 2.3+
        ssh_command = 'ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -i ' + key_path

        env = os.environ.copy()
        env['GIT_SSH_COMMAND'] = ssh_command

        return env

    @classmethod
    def _render_ssh_key(cls, pr_processor, pr_repo):
        b64_key = pr_processor.config.get('repositories', pr_processor.pull_request.repo, 'deploy_key')
        if not b64_key:
            raise RuntimeError('No deploy key for %s' % (pr_processor.pull_request.repo, ))
        key = base64.b64decode(b64_key)

        key_path = os.path.join(cls._SSH_KEYS, '%s.key' % (pr_repo, ))
        with cls._with_chdir(os.path.dirname(key_path)):
            with open(key_path, 'w') as key_file:
                key_file.write(key.decode())
        # SSH will refuse to use a key with permissions that are too open
        os.chmod(key_path, 0o600)

        return key_path

    @classmethod
    def _clone_repo_if_needed(cls, pr_repo, git_env):
        if not os.path.isdir(os.path.join(cls._REPOS_ROOT, pr_repo)):
            clone_command = 'clone git@github.com:%s.git' % (pr_repo, )
            cls._git_command(os.path.dirname(pr_repo), clone_command, env=git_env)


class CommentsRetrier(BaseRetrier):
    # returns the list of all comments made by the user we post as
    # comments are IssueComment objects
    # see https://pygithub.readthedocs.io/en/latest/github_objects/IssueComment.html
    @classmethod
    def _get_all_comments_by_user(cls, pr_processor):
        user = pr_processor.config.get('github', 'user')
        if not user:
            raise RuntimeError('Missing Github username!')

        gh_pr = cls._github_pr(pr_processor)
        return [c for c in gh_pr.get_issue_comments() if c.user.login == user]

    @classmethod
    def _post_comment(cls, pr_processor, body):
        return cls._github_pr(pr_processor).create_issue_comment(body)


class KubeRetrier(CommentsRetrier):
    def cleanup(self, pr_processor):
        self.__class__._cleanup(pr_processor)

    def retry(self, pr_processor, pr_checks_status):
        self.__class__._cleanup(pr_processor, set([check.context for check in pr_checks_status.retry_pending]))
        for check in pr_checks_status.retrying:
            self.__class__._post_comment(pr_processor, self._PREFIX + check.context)

    _PREFIX = '/test '

    @classmethod
    def _cleanup(cls, pr_processor, retry_pending=None):
        for comment in cls._get_all_comments_by_user(pr_processor):
            if not comment.body.startswith(cls._PREFIX):
                continue
            context = comment.body[len(cls._PREFIX):]
            if ' ' in context:
                continue
            if retry_pending is None or context not in retry_pending:
                comment.delete()


if __name__ == '__main__':
    from github import Github

    from pr_processor import PullRequestProcessor

    pull_request = PullRequest('kubernetes/kubernetes', 77953)
    # pull_request = PullRequest('moby/moby', 38349)
    config = Config()
    gh_client = Github(config.get('github', 'api_token'))
    processor = PullRequestProcessor(pull_request, gh_client, config)

    if False:
        GitAmendPushRetrier().retry(processor, None)

        print(pull_request.last_processed_sha)

    if False:
        print(CommentsRetrier._get_all_comments_by_user(processor))
        new_comment = CommentsRetrier._post_comment(processor, 'coucou')
        print(new_comment)
        new_comment.delete()

    if False:
        from models import Check
        from pr_processor import PullRequestChecksStatus

        retry_pending = [Check(pull_request, 'pull-kubernetes-e2e-gce-100-performance')]
        pr_checks_status = PullRequestChecksStatus([], [], [], retry_pending, [])

        KubeRetrier().retry(processor, pr_checks_status)

    if False:
        KubeRetrier().cleanup(processor)
