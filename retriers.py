class BaseRetrier(object):
    def cleanup(self, pr_processor):
        raise NotImplementedError

    def retry(self, pr_processor, pr_checks_status):
        raise NotImplementedError


class GitAmendPushRetrier(BaseRetrier):
    def cleanup(self, pr_processor):
        # nothing to do here
        pass

    def retry(self, pr_processor, pr_checks_status):
        raise NotImplementedError

    # no need to get a full blown git lib for what we do here
    @classmethod
    def _git_command(subcommand):
        pass
