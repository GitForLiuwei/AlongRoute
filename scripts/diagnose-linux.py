#!/usr/bin/env python3
"""Read-only network diagnostics. Run in the server's normal SSH terminal."""
import datetime
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / '.runtime' / 'network-diagnostic.json'
REPORT.parent.mkdir(parents=True, exist_ok=True)
secrets = []
for name in ('backend/.env', 'frontend/.env.local'):
    path = ROOT / name
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                value = line.split('=', 1)[1].strip().strip('\"\'')
                if value:
                    secrets.append(value)


def redact(text):
    for value in secrets:
        text = text.replace(value, '[REDACTED]')
    return re.sub(r'(?i)((?:key|jscode|token|authorization)=)[^\s&]+', r'\1[REDACTED]', text)


def command(args, timeout=8):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {'exit_code': result.returncode, 'output': redact((result.stdout + result.stderr)[-18000:])}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'error': redact(str(exc))}


def probe(url):
    with tempfile.TemporaryDirectory(prefix='alongroute-diag-') as folder:
        headers, body = Path(folder) / 'headers', Path(folder) / 'body'
        result = command(['curl', '--noproxy', '*', '--silent', '--show-error',
                          '--connect-timeout', '5', '--max-time', '8',
                          '--max-filesize', '1048576', '-D', str(headers), '-o', str(body), url], timeout=10)
        result['url'] = url
        result['headers'] = redact(headers.read_text(errors='replace')) if headers.exists() else ''
        result['body_preview'] = redact(body.read_text(errors='replace')[:3000]) if body.exists() else ''
        return result


report = {'time': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
          'service': command(['systemctl', 'show', 'alongroute.service',
                              '--property=ActiveState,SubState,MainPID']),
          'listener': command(['ss', '-lntp', 'sport = :8080']),
          'local_health': probe('http://127.0.0.1:8080/api/health'),
          'local_home': probe('http://127.0.0.1:8080/'),
          'public_health': probe('http://120.24.222.150:8080/api/health'),
          'public_home': probe('http://120.24.222.150:8080/'),
          'ufw': command(['ufw', 'status', 'verbose']),
          'input_rules': command(['iptables', '-S', 'INPUT']),
          'service_logs': command(['journalctl', '-u', 'alongroute.service', '--no-pager', '-n', '35'])}
REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
REPORT.chmod(0o600)
print('诊断完成，未修改服务或网络设置。')
print('报告位置：', REPORT)
for key in ('service', 'listener', 'local_health', 'public_health', 'public_home'):
    value = report[key]
    print(key + ':', value.get('error') or value.get('headers', '').splitlines()[:1] or value.get('output', '').strip())
print('告诉助手“诊断完成”，即可读取报告继续定位。')
