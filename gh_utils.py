class GithubUtils(object):
    @staticmethod
    def github_pr(gh_client, pull_request):
        return gh_client.get_repo(pull_request.repo).get_pull(pull_request.number)


class CommentsHelper(object):
    # returns the list of all comments made by the user we post as
    # comments are IssueComment objects
    # see https://pygithub.readthedocs.io/en/latest/github_objects/IssueComment.html
    @staticmethod
    def get_all_comments_by_user_on_pr(gh_client, config, pull_request):
        user = config.get('github', 'user')
        if not user:
            raise RuntimeError('Missing Github username!')

        gh_pr = GithubUtils.github_pr(gh_client, pull_request)
        return [c for c in gh_pr.get_issue_comments() if c.user.login == user]

    @staticmethod
    def post_comment(gh_client, pull_request, body):
        return GithubUtils.github_pr(gh_client, pull_request).create_issue_comment(body)
