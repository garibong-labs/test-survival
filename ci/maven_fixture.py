"""Install a checksum-pinned Maven distribution in the network-disabled fixture image."""
import hashlib
from pathlib import Path
import tarfile
import tempfile
from urllib.request import urlopen

VERSION = '3.9.9'
SHA512 = 'a555254d6b53d267965a3404ecb14e53c3827c09c3b94b5678835887ab404556bfaf78dcfe03ba76fa2508649dca8531c74bca4d5846513522404d48e8c4ac8b'


def install():
    url = f'https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/{VERSION}/apache-maven-{VERSION}-bin.tar.gz'
    with urlopen(url, timeout=120) as response, tempfile.TemporaryFile() as archive:
        while block := response.read(1024*1024): archive.write(block)
        archive.seek(0)
        if hashlib.file_digest(archive, 'sha512').hexdigest() != SHA512:
            raise ValueError('Maven distribution checksum mismatch')
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode='r:gz') as bundle:
            bundle.extractall('/opt', filter='data')
    Path('/usr/local/bin/mvn').symlink_to(f'/opt/apache-maven-{VERSION}/bin/mvn')


if __name__ == '__main__': install()
