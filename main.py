from github import Github

from config import Config
from database import init_db
from models import PullRequest
from notifiers import MailgunNotifier
from retriers import *
from pr_processor import PullRequestProcessor


class Main(object):
    @classmethod
    def run(cls):
        config = Config()
        gh_client = cls._github_client(config)
        db_session = init_db()

        all_gh_prs = cls._fetch_prs(gh_client, config)
        existing_prs = cls._existing_db_prs(db_session)

        to_process, to_cleanup = cls._triage_prs(config, all_gh_prs, existing_prs)

        cls._process_prs(to_process, gh_client, config, db_session)
        cls._cleanup_prs(to_cleanup, gh_client, config, db_session)

    @staticmethod
    def _fetch_prs(gh_client, config):
        user = config.get('github', 'user')
        if not user:
            raise RuntimeError('Missing Github username!')

        query = 'is:open is:pr author:%s archived:false' % (user, )

        return [PullRequest.from_url(issue.html_url)
                for issue in gh_client.search_issues(query)]

    @staticmethod
    def _github_client(config):
        token = config.get('github', 'api_token')
        if not token:
            raise RuntimeError('Missing Github API token!')
        return Github(token)

    @staticmethod
    def _existing_db_prs(db_session):
        return {pr.slug: pr for pr in db_session.query(PullRequest).all()}

    @classmethod
    def _triage_prs(cls, config, all_gh_prs, existing_prs):
        to_process = []

        for pr in all_gh_prs:
            repo_config = cls._repo_config(config, pr)
            if repo_config is None:
                continue

            existing_pr = existing_prs.pop(pr.slug, None)
            if existing_pr:
                pr = existing_pr

            to_process.append(pr)

        return to_process, existing_prs.values()

    @classmethod
    def _process_prs(cls, to_process, gh_client, config, db_session):
        notifier = MailgunNotifier()

        for pr in to_process:
            processor = PullRequestProcessor(pr, gh_client, config)
            processor.run(db_session, cls._build_retrier(config, pr), notifier)

    @classmethod
    def _cleanup_prs(cls, to_cleanup, gh_client, config, db_session):
        for pr in to_cleanup:
            processor = PullRequestProcessor(pr, gh_client, config)
            retrier = cls._build_retrier(config, pr)
            retrier.cleanup(processor)

            db_session.delete(pr)

    @classmethod
    def _build_retrier(cls, config, pr):
        retrier_class = cls._repo_config(config, pr, 'retrier_class')
        if retrier_class:
            # courtesy of https://stackoverflow.com/a/1176179
            retrier_class = globals()[retrier_class]
        else:
            retrier_class = GitAmendPushRetrier

        return retrier_class()

    @staticmethod
    def _repo_config(config, pr, *subpath):
        return config.get('repositories', pr.repo, *subpath)


if __name__ == '__main__':
    Main.run()
