from collections import namedtuple
import datetime
import tempfile

from mock import patch
import pytest

from database import init_db
from config import Config
from models import Check, PullRequest
from pr_processor import Datetime, PullRequestProcessor
from retriers import BaseRetrier
from test_helpers import fixture_path, Generator


class FakeGithubClient(object):
    # shas should be a list of strings that will be returned as the successive
    # SHAs
    # gh_checks should be a list of list of gh_checks; the 1st one will be the
    # first one returned, each successive one being the additive delta from the
    # previous reply
    # checks should be 3-tuples (context, state, id)
    def __init__(self, pull_request, shas, gh_checks):
        self._pr = pull_request
        self._repo = FakeGithubRepo(pull_request, shas, gh_checks)

    def get_repo(self, repo):
        assert repo == self._pr.repo
        return self._repo

    def assert_exhausted(self):
        self._repo.assert_exhausted()


class FakeGithubRepo(object):
    def __init__(self, pull_request, shas, gh_checks):
        self._pr = pull_request
        self._shas_generator = Generator(*shas)
        self._gh_checks_generator = Generator(*gh_checks)
        self._gh_checks_so_far = []

    _fake_github_pull = namedtuple('FakeGithubPull', 'head')
    _fake_github_head = namedtuple('FakeGithubHead', 'sha')

    def get_pull(self, pr_number):
        assert pr_number == self._pr.number

        head = self._fake_github_head(self._shas_generator.next())
        return self._fake_github_pull(head)

    _fake_github_commit = namedtuple('FakeGithubCommit', 'get_statuses')
    _fake_github_check = namedtuple('FakeGithubCheck', ('context', 'state', 'id'))

    def get_commit(self, sha):
        if self._shas_generator.index < 0:
            raise RuntimeError('Calling get_commit before get_pull')
        assert sha == self._shas_generator.last_value

        new_gh_checks = [self._fake_github_check(*c) for c in self._gh_checks_generator.next()]
        self._gh_checks_so_far = new_gh_checks + self._gh_checks_so_far

        return self._fake_github_commit(lambda: self._gh_checks_so_far)

    def assert_exhausted(self):
        self._shas_generator.assert_exhausted()
        self._gh_checks_generator.assert_exhausted()


class FakeRetrier(BaseRetrier):
    def __init__(self, pr_processor, retry_func=None):
        self._pr_processor = pr_processor
        self._retry_func = retry_func
        self._cleanup_count = 0
        self._retried = []

    def cleanup(self, pr_processor):
        assert pr_processor is self._pr_processor
        self._cleanup_count += 1

    @property
    def cleanup_count(self):
        return self._cleanup_count

    def retry(self, pr_processor, pr_checks_status):
        assert pr_processor is self._pr_processor
        self._retried.append(pr_checks_status)
        if self._retry_func:
            self._retry_func(pr_processor, pr_checks_status)

    @property
    def retried(self):
        return self._retried


class FakeNotifier(object):
    def __init__(self, pr_processor):
        self._pr_processor = pr_processor
        self._too_many_failures = []
        self._retrying = []
        self._success = []

    def __getattr__(self, method):
        def func(*args):
            arr = getattr(self, '_' + method)
            if len(args) == 2:
                pr_processor, pr_checks_status = args
                assert pr_processor is self._pr_processor
                arr.append(pr_checks_status)
            elif len(args) == 0:
                return arr
            else:
                raise RuntimeError('Unexpected args to %s.%s: %s' % (self.__class__.__name__, method, args))

        return func

    def __len__(self):
        return len(self._too_many_failures) + len(self._retrying) + len(self._success)


@pytest.fixture
def default_config():
    return Config(fixture_path('config.yml'))


@pytest.fixture
def db_session():
    with tempfile.NamedTemporaryFile() as temp_db:
        yield init_db(temp_db.name)


# actual should be a PullRequesdt object
# expected should be a (last_processed_sha, status) tuple
def assert_pr_equal(pull_request, actual, expected):
    assert actual.repo == pull_request.repo
    assert actual.number == pull_request.number
    assert actual.last_processed_sha == expected[0]
    assert actual.status == expected[1]


# actual is a list of Check objects
# expected is a list of (name, last_errored_id, failure_count) tuples or just a
# single name if no failure
def assert_checks_equal(pull_request, actual, expected):
    assert len(actual) == len(expected)
    for i, check in enumerate(actual):
        assert check.repo == pull_request.repo
        assert check.number == pull_request.number
        if isinstance(expected[i], tuple):
            assert check.context == expected[i][0]
            assert check.last_errored_id == expected[i][1]
            assert check.failure_count == expected[i][2]
        elif isinstance(expected[i], str):
            assert check.context == expected[i]
            assert check.last_errored_id is None
            assert check.failure_count == 0
        else:
            raise RuntimeError('Unexpected argument to assert_checks_equal: %s' % (expected[i], ))


def test_basic_retry(db_session, default_config):
    pull_request = PullRequest('moby/moby', 34567)
    gh_client = FakeGithubClient(pull_request, ['1' * 40], [[('coucou', 'pending', 12), ('blah', 'error', 28)]])

    processor = PullRequestProcessor(pull_request, gh_client, default_config)
    retrier = FakeRetrier(processor)
    notifier = FakeNotifier(processor)

    processor.run(db_session, retrier, notifier)
    gh_client.assert_exhausted()

    assert_pr_equal(pull_request, pull_request, ('1' * 40, 'pending'))

    assert len(retrier.retried) == 1
    assert_checks_equal(pull_request, retrier.retried[0].retrying, [('blah', 28, 1)])
    assert_checks_equal(pull_request, retrier.retried[0].pending, ['coucou'])
    assert len(retrier.retried[0]) == 2
    assert retrier.cleanup_count == 0

    assert len(notifier.retrying()) == 1
    assert notifier.retrying()[0] is retrier.retried[0]
    assert len(notifier) == 1

    # let's look at what's in the DB
    assert_pr_equal(pull_request, db_session.query(PullRequest).all()[0], ('1' * 40, 'pending'))
    assert_checks_equal(pull_request, db_session.query(Check).all(), ['coucou', ('blah', 28, 1)])


def test_pending_retry_checks_are_left_alone(db_session, default_config):
    pull_request = PullRequest('moby/moby', 34567)
    gh_client = FakeGithubClient(
        pull_request,
        ['1' * 40, '1' * 40, '1' * 40],
        [[('coucou', 'pending', 12), ('blah', 'error', 28)], [], [('coucou', 'error', 12)]]
    )

    processor = PullRequestProcessor(pull_request, gh_client, default_config)
    retrier = FakeRetrier(processor)
    notifier = FakeNotifier(processor)

    # then we run twice
    processor.run(db_session, retrier, notifier)
    processor.run(db_session, retrier, notifier)

    assert_pr_equal(pull_request, pull_request, ('1' * 40, 'pending'))

    # everything should be the same as if we had just run once
    assert len(retrier.retried) == 1
    assert_checks_equal(pull_request, retrier.retried[0].retrying, [('blah', 28, 1)])
    assert_checks_equal(pull_request, retrier.retried[0].pending, ['coucou'])
    assert len(retrier.retried[0]) == 2
    assert retrier.cleanup_count == 0

    assert len(notifier.retrying()) == 1
    assert notifier.retrying()[0] is retrier.retried[0]
    assert len(notifier) == 1

    # let's look at what's in the DB
    assert_pr_equal(pull_request, db_session.query(PullRequest).all()[0], ('1' * 40, 'pending'))
    assert_checks_equal(pull_request, db_session.query(Check).all(), ['coucou', ('blah', 28, 1)])

    # now let's run a 3rd time, 'coucou' fails
    processor.run(db_session, retrier, notifier)
    gh_client.assert_exhausted()

    assert len(retrier.retried) == 2
    assert_checks_equal(pull_request, retrier.retried[1].retrying, [('coucou', 12, 1)])
    assert_checks_equal(pull_request, retrier.retried[1].retry_pending, [('blah', 28, 1)])
    assert len(retrier.retried[1]) == 2
    assert retrier.cleanup_count == 0


def test_too_many_failures(db_session, default_config):
    pull_request = PullRequest('moby/moby', 34567)
    gh_client = FakeGithubClient(
        pull_request,
        ['1' * 40, '1' * 40, '1' * 40],
        [[('coucou', 'pending', 12), ('fast_fail', 'error', 28)], [('fast_fail', 'error', 82)]]
    )

    processor = PullRequestProcessor(pull_request, gh_client, default_config)
    retrier = FakeRetrier(processor)
    notifier = FakeNotifier(processor)

    # then we run twice
    processor.run(db_session, retrier, notifier)
    processor.run(db_session, retrier, notifier)

    # we should only have retried once
    assert len(retrier.retried) == 1
    assert_checks_equal(pull_request, retrier.retried[0].retrying, [('fast_fail', 28, 1)])
    assert_checks_equal(pull_request, retrier.retried[0].pending, ['coucou'])
    assert len(retrier.retried[0]) == 2
    # and we should have cleaned up
    assert retrier.cleanup_count == 1

    assert len(notifier.retrying()) == 1
    assert notifier.retrying()[0] is retrier.retried[0]
    assert len(notifier.too_many_failures()) == 1
    assert_checks_equal(pull_request, notifier.too_many_failures()[0].too_many_failures, [('fast_fail', 82, 2)])
    assert_checks_equal(pull_request, notifier.too_many_failures()[0].pending, ['coucou'])
    assert len(notifier.too_many_failures()[0]) == 2
    assert len(notifier) == 2

    assert_pr_equal(pull_request, db_session.query(PullRequest).all()[0], ('1' * 40, 'failed'))

    # running again should not do anything
    processor.run(db_session, retrier, notifier)
    gh_client.assert_exhausted()


def test_detect_success(db_session, default_config):
    pull_request = PullRequest('moby/moby', 34567)
    gh_client = FakeGithubClient(
        pull_request,
        ['1' * 40, '1' * 40, '1' * 40],
        [[('coucou', 'success', 12), ('blah', 'pending', 28)], [('blah', 'success', 28)]]
    )

    processor = PullRequestProcessor(pull_request, gh_client, default_config)
    retrier = FakeRetrier(processor)
    notifier = FakeNotifier(processor)

    # then we run twice
    processor.run(db_session, retrier, notifier)
    processor.run(db_session, retrier, notifier)

    # we shouldn't have retried anything
    assert len(retrier.retried) == 0
    # but we should have cleaned up
    assert retrier.cleanup_count == 1

    # and let's check in the DB
    assert_pr_equal(pull_request, db_session.query(PullRequest).all()[0], ('1' * 40, 'successful'))
    assert_checks_equal(pull_request, db_session.query(Check).all(), ['coucou', 'blah'])

    # running again should not do anything
    processor.run(db_session, retrier, notifier)
    gh_client.assert_exhausted()


def test_pending_retry_checks_are_retriggered_after_a_while(db_session, default_config):
    with patch.object(Datetime, 'now') as patched_now:
        generator = Generator(datetime.datetime(2019, 1, 1, 12, 12),
                              datetime.datetime(2019, 1, 1, 12, 18))
        patched_now.side_effect = generator.next

        pull_request = PullRequest('moby/moby', 34567)
        gh_client = FakeGithubClient(
            pull_request,
            ['1' * 40, '1' * 40],
            [[('coucou', 'pending', 12), ('blah', 'error', 28)], []]
        )

        processor = PullRequestProcessor(pull_request, gh_client, default_config)
        retrier = FakeRetrier(processor)
        notifier = FakeNotifier(processor)

        # then we run twice
        processor.run(db_session, retrier, notifier)
        processor.run(db_session, retrier, notifier)
        gh_client.assert_exhausted()
        generator.assert_exhausted()

        # we should have retried twice
        assert len(retrier.retried) == 2
        for retried in retrier.retried:
            assert_checks_equal(pull_request, retried.retrying, [('blah', 28, 1)])
            assert_checks_equal(pull_request, retried.pending, ['coucou'])
            assert len(retrier.retried[0]) == 2
        assert retrier.cleanup_count == 0


def test_retrier_can_alter_db_objects(db_session, default_config):
    pull_request = PullRequest('moby/moby', 34567)
    gh_client = FakeGithubClient(pull_request, ['1' * 40], [[('coucou', 'pending', 12), ('blah', 'error', 28)]])

    processor = PullRequestProcessor(pull_request, gh_client, default_config)

    def retry_func(pr_processor, pr_checks_status):
        pr_processor.pull_request.last_processed_sha = '3' * 40
        assert len(pr_checks_status.retrying) == 1
        pr_checks_status.retrying[0].last_errored_id = 82
    retrier = FakeRetrier(processor, retry_func=retry_func)
    notifier = FakeNotifier(processor)

    processor.run(db_session, retrier, notifier)
    gh_client.assert_exhausted()

    # let's look at what's in the DB
    assert_pr_equal(pull_request, db_session.query(PullRequest).all()[0], ('3' * 40, 'pending'))
    assert_checks_equal(pull_request, db_session.query(Check).all(), ['coucou', ('blah', 82, 1)])


def test_resume_after_failure_if_new_patch(db_session, default_config):
    pull_request = PullRequest('moby/moby', 34567)
    gh_client = FakeGithubClient(
        pull_request,
        ['1' * 40, '1' * 40, '2' * 40],
        [[('coucou', 'pending', 12), ('fast_fail', 'error', 28)],
         [('fast_fail', 'error', 82)], [('coucou', 'pending', 13), ('fast_fail', 'error', 93)]]
    )

    processor = PullRequestProcessor(pull_request, gh_client, default_config)
    retrier = FakeRetrier(processor)
    notifier = FakeNotifier(processor)

    # then we run twice
    processor.run(db_session, retrier, notifier)
    processor.run(db_session, retrier, notifier)

    # we should only have retried once
    assert len(retrier.retried) == 1
    assert_checks_equal(pull_request, retrier.retried[0].retrying, [('fast_fail', 28, 1)])
    assert_checks_equal(pull_request, retrier.retried[0].pending, ['coucou'])
    assert len(retrier.retried[0]) == 2
    # and we should have cleaned up
    assert retrier.cleanup_count == 1

    assert_pr_equal(pull_request, db_session.query(PullRequest).all()[0], ('1' * 40, 'failed'))

    # now let's run again, it's a new patch
    processor.run(db_session, retrier, notifier)
    gh_client.assert_exhausted()

    # we should have retried again
    assert len(retrier.retried) == 2
    assert_checks_equal(pull_request, retrier.retried[1].retrying, [('fast_fail', 93, 1)])
    assert_checks_equal(pull_request, retrier.retried[1].pending, ['coucou'])
    assert len(retrier.retried[0]) == 2

    assert_pr_equal(pull_request, db_session.query(PullRequest).all()[0], ('2' * 40, 'pending'))


def test_resume_after_success_if_new_patch(db_session, default_config):
    pull_request = PullRequest('moby/moby', 34567)
    gh_client = FakeGithubClient(
        pull_request,
        ['1' * 40, '1' * 40, '2' * 40],
        [[('coucou', 'success', 12), ('blah', 'pending', 28)],
         [('blah', 'success', 28)], [('coucou', 'error', 13), ('blah', 'pending', 93)]]
    )

    processor = PullRequestProcessor(pull_request, gh_client, default_config)
    retrier = FakeRetrier(processor)
    notifier = FakeNotifier(processor)

    # then we run twice
    processor.run(db_session, retrier, notifier)
    processor.run(db_session, retrier, notifier)

    # we shouldn't have retried anything
    assert len(retrier.retried) == 0
    # but we should have cleaned up
    assert retrier.cleanup_count == 1

    assert_pr_equal(pull_request, db_session.query(PullRequest).all()[0], ('1' * 40, 'successful'))

    # now let's run again, it's a new patch
    processor.run(db_session, retrier, notifier)
    gh_client.assert_exhausted()

    # we should now have retried
    assert len(retrier.retried) == 1
    assert_checks_equal(pull_request, retrier.retried[0].retrying, [('coucou', 13, 1)])
    assert_checks_equal(pull_request, retrier.retried[0].pending, ['blah'])
    assert len(retrier.retried[0]) == 2

    assert_pr_equal(pull_request, db_session.query(PullRequest).all()[0], ('2' * 40, 'pending'))


def test_it_ignores_checks_marked_as_such(db_session, default_config):
    pull_request = PullRequest('moby/moby', 34567)
    gh_client = FakeGithubClient(
        pull_request, ['1' * 40, '1' * 40], [[('coucou', 'success', 12), ('codecov/patch', 'error', 28)]])

    processor = PullRequestProcessor(pull_request, gh_client, default_config)
    retrier = FakeRetrier(processor)
    notifier = FakeNotifier(processor)

    processor.run(db_session, retrier, notifier)

    assert_pr_equal(pull_request, pull_request, ('1' * 40, 'successful'))

    # we shouldn't have retried anything
    assert len(retrier.retried) == 0
    # but we should have cleaned up
    assert retrier.cleanup_count == 1

    # and let's check in the DB
    assert_pr_equal(pull_request, db_session.query(PullRequest).all()[0], ('1' * 40, 'successful'))
    assert_checks_equal(pull_request, db_session.query(Check).all(), ['coucou'])

    # running again should not do anything
    processor.run(db_session, retrier, notifier)
    gh_client.assert_exhausted()
