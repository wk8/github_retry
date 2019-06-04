import datetime
import re

from beautifultable import BeautifulTable

from models import Check


# TODO wkpo cleanup les DB objects created?

# needed for mocking in tests - can't mock datetime.datetime.now directly
class Datetime(datetime.datetime):
    pass


class PullRequestChecksStatus(object):
    # all of these should be list of Check objects
    def __init__(self, successful, pending, retrying, retry_pending, too_many_failures):
        self.successful = successful
        self.pending = pending
        self.retrying = retrying
        self.retry_pending = retry_pending
        self.too_many_failures = too_many_failures

    def to_list(self):
        return self.successful + self.pending + self.retrying + self.retry_pending + self.too_many_failures

    def __len__(self):
        return len(self.successful) + len(self.pending) + len(self.retrying) \
            + len(self.retry_pending) + len(self.too_many_failures)

    def __repr__(self):
        table = BeautifulTable()
        table.column_headers = ['name', 'status', 'failure count', 'last retried at']
        for status in ['successful', 'pending', 'retrying', 'retry_pending', 'too_many_failures']:
            pretty_status = status.replace('_', ' ')
            for check in getattr(self, status):
                retried_at = check.last_retried_at if check.last_retried_at else '-'
                table.append_row([check.context, pretty_status, check.failure_count, retried_at])
        return '\n' + str(table) + '\n'


class PullRequestProcessor(object):
    DEFAULT_MAX_RETRIES = 7
    DEFAULT_MAX_RETRY_DELAY = 5  # in minutes

    def __init__(self, pr, gh_client, config):
        self._pr = pr
        self._gh_client = gh_client
        self._config = config

    def run(self, db_session, retrier, notifier):
        head_sha = self._head_sha()
        new_patch = head_sha != self._pr.last_processed_sha
        self._pr.last_processed_sha = head_sha
        if new_patch:
            self._pr.status = 'pending'
        elif self._pr.status != 'pending':
            return

        existing_db_checks = self._existing_db_checks(db_session)
        current_gh_checks = self._fetch_current_gh_checks(head_sha)
        now = Datetime.now()
        checks = self._update_or_create_db_checks(existing_db_checks, current_gh_checks, new_patch, now)

        if checks.too_many_failures:
            self._pr.status = 'failed'
            notifier.too_many_failures(self, checks)
            retrier.cleanup(self)
        elif checks.retrying:
            notifier.retrying(self, checks)
            retrier.retry(self, checks)

            for check in checks.retrying:
                check.last_retried_at = now
        elif len(checks.retry_pending) + len(checks.pending) == 0:
            self._pr.status = 'successful'
            notifier.success(self, checks)
            retrier.cleanup(self)

        db_session.add(self._pr)
        db_session.bulk_save_objects(checks.to_list())
        db_session.commit()

    @property
    def pull_request(self):
        return self._pr

    @property
    def client(self):
        return self._gh_client

    @property
    def config(self):
        return self._config

    def _head_sha(self):
        gh_pr = self.client.get_repo(self._pr.repo).get_pull(self._pr.number)
        return gh_pr.head.sha

    # returns a dict mapping the check contexts to the objects
    def _existing_db_checks(self, db_session):
        result = {}

        checks = db_session.query(Check).filter(Check.repo == self._pr.repo).filter(Check.number == self._pr.number)
        for check in checks:
            result[check.context] = check

        return result

    # fetches the current checks from GH
    # only keeps the latest one for each context
    def _fetch_current_gh_checks(self, head_sha):
        # from
        # https://developer.github.com/v3/repos/statuses/#list-statuses-for-a-specific-ref
        # "Statuses are returned in reverse chronological order. The first
        # status in the list will be the latest one."
        gh_commit = self.client.get_repo(self._pr.repo).get_commit(head_sha)

        result = []
        seen_contexts = set()

        for status in gh_commit.get_statuses():
            if status.context in seen_contexts:
                continue
            seen_contexts.add(status.context)

            result.append(status)

        return result

    # updates or creates the DB check objects with the latest info
    # doesn't actually save anything to the DB just yet
    # returns a PullRequestChecksStatus
    def _update_or_create_db_checks(self, existing_db_checks, current_gh_checks, new_patch, now):
        successful = []
        pending = []
        retrying = []
        retry_pending = []
        too_many_failures = []

        for gh_check in current_gh_checks:
            if self._check_config(gh_check.context, 'ignore'):
                continue

            if gh_check.context in existing_db_checks:
                check = existing_db_checks[gh_check.context]

                if new_patch:
                    check.failure_count = 0
            else:
                check = Check(self._pr, gh_check.context)

            if gh_check.state == 'error':
                max_retries = self._resolve_multi_level_config('max_retries', gh_check.context,
                                                               self.DEFAULT_MAX_RETRIES)

                if gh_check.id == check.last_errored_id:
                    if check.failure_count > max_retries:
                        too_many_failures.append(check)
                    else:
                        delay = self._resolve_multi_level_config('max_retry_delay', gh_check.context,
                                                                 self.DEFAULT_MAX_RETRY_DELAY)
                        if not check.last_retried_at or \
                                now - check.last_retried_at > datetime.timedelta(minutes=delay):
                            retrying.append(check)
                        else:
                            retry_pending.append(check)
                else:
                    # new failure
                    check.failure_count += 1
                    check.last_errored_id = gh_check.id

                    if check.failure_count > max_retries:
                        too_many_failures.append(check)
                    else:
                        retrying.append(check)

            elif gh_check.state == 'pending':
                pending.append(check)

            elif gh_check.state == 'success':
                successful.append(check)

            else:
                raise RuntimeError('Unknown check status %s for check %s in %s/%s' %
                                   (gh_check.state, gh_check.context, self._pr.repo, self._pr.number))

        return PullRequestChecksStatus(successful, pending, retrying, retry_pending, too_many_failures)

    # resolves a config parameter than can be set both on the repo or on
    # the check (or even at the root level)
    def _resolve_multi_level_config(self, key, check_name, default):
        check_level = self._check_config(check_name, key)
        if check_level:
            return check_level
        repo_level = self._repo_config(key)
        if repo_level:
            return repo_level
        root_level = self.config.get(key)
        if root_level:
            return root_level
        return default

    def _check_config(self, check_name, *subpath):
        return self._repo_config('checks', check_name, *subpath)

    def _repo_config(self, *subpath):
        return self.config.get('repositories', self._pr.repo, *subpath)
