import ssl
import enum
import socket
import certifi
from concurrent import futures
import attr
from oscrypto.errors import TLSError
from oscrypto.tls import TLSSocket
from .url import parse_url


def openssl_check_hostname(hostname):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    context = ssl.create_default_context(cafile=certifi.where())
    ssl_sock = context.wrap_socket(sock, server_hostname=hostname)
    try:
        ssl_sock.connect((hostname, 443))
        ssl_sock.shutdown(socket.SHUT_RDWR)
        ssl_sock.close()
    except socket.timeout as e:
        return 'Timed out'
    except OSError as e:
        return parse_socket_error_message(e.args[1])
    except ssl.CertificateError as e:
        return str(e)
    else:
        return None


def parse_socket_error_message(message):
    # example: "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert handshake failure (_ssl.c:749)"
    start_ind = message.find(']')
    if start_ind > -1:
        start = start_ind + 2
        end = message.find('(_ssl') - 1
        message = message[start:end]
    return message


def oscrypto_check_hostname(hostname):
    try:
        # TODO: enable certificate verification with certifi (Mozilla CA bundle)
        # to make results more consistent and reproducible
        tls_socket = TLSSocket(hostname, 443)
        tls_socket.shutdown()
    except TLSError as e:
        return e.message
    except socket.gaierror as e:
        return (str(e))
    except socket.timeout as e:
        return 'Timed out'
    else:
        return None


class CheckSiteManager:
    def __init__(self, urls, redirect, timeout, retries):
        self.redirect = redirect
        self.timeout = timeout
        self.retries = retries
        self.urls = urls
        self.skipped = []
        self.succeeded = []
        self.failed = []

    def check_sites(self):
        skipped_urls = self._skip_urls()
        hostnames = {parse_url(url).host for url in self.urls if url not in skipped_urls}

        with futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures_to_urls = {executor.submit(self._check, hostname) for hostname in hostnames}
            # we start yielding after starting requests, so the perceived speed might be better
            # if the client does something with the return values
            yield from self.skipped
            for future in futures.as_completed(futures_to_urls):
                result = self._make_result(future)
                if result.succeeded:
                    self.succeeded.append(result)
                elif result.failed:
                    self.failed.append(result)
                yield result

    @property
    def success_count(self):
        return len(self.succeeded)

    @property
    def skip_count(self):
        return len(self.skipped)

    @property
    def fail_count(self):
        return len(self.failed)

    def _skip_urls(self):
        skipped_urls = set()

        for url in self.urls:
            purl = parse_url(url)
            if not purl.host or not purl.host.strip():
                self._skip(url, 'invalid hostname')
                skipped_urls.add(url)
            # any other protocoll will be None and as we cannot make a difference,
            # we will check those. Maybe we shouldn't?
            elif purl.scheme == 'http':
                self._skip(url, 'not https://')
                skipped_urls.add(url)

        return skipped_urls

    def _skip(self, url, reason):
        skipresult = CheckedSite(url, CheckResult.SKIPPED, reason)
        self.skipped.append(skipresult)

    def _make_result(self, future):
        hostname, error_message = future.result()
        result = CheckResult.FAILED if error_message else CheckResult.SUCCEEDED
        return CheckedSite(hostname, result, error_message)

    def _check(self, hostname):
        # OpenSSL is more strict about misconfigured servers, e.g. it recognizes missing chains
        openssl_error = openssl_check_hostname(hostname)
        if not openssl_error:
            return hostname, None
        # Timeout is the same for both, don't do it twice unnecessary
        elif openssl_error == 'Timed out':
            return hostname, openssl_error
        else:
            # OSCrypto gives better error messages, but is more allowing
            oscrypto_error = oscrypto_check_hostname(hostname)
            return hostname, oscrypto_error or openssl_error


class CheckResult(enum.Enum):
    SUCCEEDED = 'SUCCEEDED'
    SKIPPED = 'SKIPPED'
    FAILED = 'FAILED'


@attr.s(slots=True, cmp=False)
class CheckedSite:
    url = attr.ib()
    result = attr.ib(convert=CheckResult)
    message = attr.ib(default=None)
    succeeded = attr.ib(init=False)
    skipped = attr.ib(init=False)
    failed = attr.ib(init=False)

    def __attrs_post_init__(self):
        self.succeeded = (self.result == CheckResult.SUCCEEDED)
        self.skipped = (self.result == CheckResult.SKIPPED)
        self.failed = (self.result == CheckResult.FAILED)
