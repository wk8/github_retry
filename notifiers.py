import requests

from config import Config


class BaseNotifier(object):
    def too_many_failures(self, pr_processor, pr_checks_status):
        raise NotImplementedError

    def retrying(self, pr_processor, pr_checks_status):
        raise NotImplementedError

    def success(self, pr_processor, pr_checks_status):
        raise NotImplementedError


class MailgunNotifier(BaseNotifier):
    '''
    Sends email notifications through Mailgun
    See https://documentation.mailgun.com/en/latest/api-sending.html#sending
    '''
    def too_many_failures(self, pr_processor, pr_checks_status):
        self.__class__._notify('FAILED', pr_processor, pr_checks_status)

    def retrying(self, pr_processor, pr_checks_status):
        self.__class__._notify('Retrying', pr_processor, pr_checks_status)

    def success(self, pr_processor, pr_checks_status):
        self.__class__._notify('SUCCESS', pr_processor, pr_checks_status)

    @classmethod
    def _notify(cls, overall_status, pr_processor, pr_checks_status):
        subject = '%s %s' % (overall_status, pr_processor.pull_request.slug)
        cls._send_email(pr_processor.config, subject, str(pr_checks_status))

    _BASE_URL = 'https://api.mailgun.net/v3/%s/messages'

    # see https://github.com/nicholaskajoh/mail-bazooka/blob/master/main.py
    @classmethod
    def _send_email(cls, config, subject, body):
        url = cls._BASE_URL % (cls._config(config, 'domain'), )
        requests.post(
            url,
            auth=('api', cls._config(config, 'api_key')),
            data={
                'from': cls._config(config, 'from'),
                'to': cls._config(config, 'to'),
                'subject': subject,
                'text': body
            }
        )

    @staticmethod
    def _config(config, *subpath):
        value = config.get('mailgun', *subpath)
        if not value:
            raise RuntimeError('Missing mailgun config: %s' % (subpath, ))
        return value


if __name__ == '__main__':
    # no unit tests here, just this for quick & dirty testing
    config = Config()
    MailgunNotifier._send_email(config, 'coucou', 'Pô')
