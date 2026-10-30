"""Create local credentials once, preserving them across subsequent starts."""

import os
from pathlib import Path
import secrets


def main():
    path = Path(__file__).with_name('.env')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print('Existing .env retained.')
        return
    with os.fdopen(fd, 'w') as target:
        for name in ('PATRONI_SUPERUSER_PASSWORD', 'PATRONI_REPLICATION_PASSWORD'):
            target.write(f'{name}={secrets.token_hex(24)}\n')
    print('Created .env with local credentials (0600).')


if __name__ == '__main__':
    main()
