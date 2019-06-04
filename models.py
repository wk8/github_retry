import re

from sqlalchemy import Column, DateTime, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import validates

from database import Base


# it seems that github has a limit of:
# * 39 chars for usernames (according to https://github.com/shinnn/github-username-regex)
# * 100 chars for repo names (according to https://github.com/evalEmpire/gitpan/issues/123)
MAX_REPO_LENGTH = 140


class PullRequest(Base):
    __tablename__ = 'pull_requests'

    # courtesy of the same repo as for MAX_REPO_LENGTH
    _REPO_REGEX = re.compile(r'^[a-z\d](?:[a-z\d]|-(?=[a-z\d])){0,38}/[a-z\d](?:[a-z\d]|-(?=[a-z\d])){0,99}$', re.IGNORECASE)  # noqa
    _URL_REGEX = re.compile(r'^(?:https://)?github.com/(%s)/pull/([0-9]+)$' % (_REPO_REGEX.pattern), re.IGNORECASE)

    repo = Column(String(MAX_REPO_LENGTH), primary_key=True)
    number = Column(Integer, primary_key=True)

    last_processed_sha = Column(String(40))

    STATUSES = ['successful', 'pending', 'failed']
    status = Column(String(max([len(status) for status in STATUSES])))

    # repo is eg moby/moby
    # number is eg 34567
    def __init__(self, repo, number):
        self.repo = repo
        self.number = int(number)

    @classmethod
    def from_url(cls, url):
        match = cls._URL_REGEX.match(url)
        if not match:
            raise RuntimeError('Not a valid PR URL: %s' % (url, ))

        return cls(match[1], match[2])

    @validates('repo')
    def _validate_repo(self, _key, repo):
        if not self._REPO_REGEX.match(repo):
            raise AssertionError

        return repo

    @validates('status')
    def _validate_repo(self, _key, status):
        if status not in self.STATUSES:
            raise AssertionError

        return status


class Check(Base):
    __tablename__ = 'checks'

    __table_args__ = (ForeignKeyConstraint(['repo', 'number'], ['pull_requests.repo', 'pull_requests.number']), )

    repo = Column(String(MAX_REPO_LENGTH), primary_key=True)
    number = Column(Integer, primary_key=True)
    context = Column(String(255), primary_key=True)

    # counts how many failures have been observed in a row,
    # that is, _consecutive_ failures
    failure_count = Column(Integer)

    # that's the ID of the last processed GH event
    last_errored_id = Column(Integer)

    # the last time this check got retried _after a failure_
    last_retried_at = Column(DateTime())

    def __init__(self, pr, context):
        self.repo = pr.repo
        self.number = pr.number
        self.context = context
        self.failure_count = 0
