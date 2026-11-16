"""Update only the existing server's upload limit, preserving other settings."""
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile


LIMIT_MIB = 900


def update_config(path):
    path = Path(path)
    original = path.read_bytes()
    settings = json.loads(original.decode('utf-8-sig'))
    if not isinstance(settings, dict):
        raise ValueError('Configuration must be a JSON object')
    if settings.get('file_send_max_mib') == LIMIT_MIB:
        return None
    settings['file_send_max_mib'] = LIMIT_MIB
    data = (json.dumps(settings, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    backup = path.with_name(path.name + '.before-upload-limit-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
    with backup.open('xb') as stream:
        stream.write(original)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != original:
            raise RuntimeError('Configuration changed during update; no replacement was made')
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return backup


if __name__ == '__main__':
    config = Path(__file__).resolve().parents[1] / 'wechat.codex.json'
    backup = update_config(config)
    print(f'Upload limit: {LIMIT_MIB} MiB ({LIMIT_MIB * 1024**2} bytes).')
    print('Configuration: ' + str(config))
    print('Backup: ' + str(backup) if backup else 'Already configured; no change.')
