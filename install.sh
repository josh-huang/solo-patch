#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo '请用 sudo bash install.sh 运行安装脚本。' >&2
    exit 1
fi

if [[ ! -d /run/systemd/system ]] || ! command -v apt-get >/dev/null; then
    echo '安装脚本需要使用 systemd 的 Ubuntu 或 Debian。' >&2
    exit 1
fi

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for file in alwayson.py store.py worker.py github_tasks.py github.conf alwayson-cli alwayson.service alwayson.conf; do
    if [[ ! -f "$source_dir/$file" ]]; then
        echo "安装包中缺少 $file" >&2
        exit 1
    fi
done

apt-get update
apt-get install -y ca-certificates python3 python3-psycopg2 postgresql
systemctl enable --now postgresql.service

if ! id -u alwayson >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin --user-group alwayson
fi

runuser -u postgres -- /usr/bin/psql -X --no-password -v ON_ERROR_STOP=1 \
    -h /var/run/postgresql -d postgres <<'SQL'
SELECT 'CREATE ROLE alwayson LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'alwayson') \gexec
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'alwayson'
              AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR NOT rolcanlogin))
    THEN
        RAISE EXCEPTION 'Existing PostgreSQL role alwayson does not match the expected service role';
    END IF;
    IF EXISTS (SELECT FROM pg_database WHERE datname = 'alwayson'
               AND datdba != (SELECT oid FROM pg_roles WHERE rolname = 'alwayson'))
    THEN
        RAISE EXCEPTION 'Existing database alwayson belongs to another owner';
    END IF;
END
$$;
SELECT 'CREATE DATABASE alwayson OWNER alwayson'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'alwayson') \gexec
SQL

install -d -m 0755 /opt/alwayson /usr/local/bin
install -m 0644 "$source_dir/alwayson.py" "$source_dir/store.py" "$source_dir/worker.py" "$source_dir/github_tasks.py" /opt/alwayson
install -m 0755 "$source_dir/alwayson-cli" /usr/local/bin/alwayson
install -m 0644 "$source_dir/alwayson.service" /etc/systemd/system/alwayson.service
install -d -m 0700 /etc/alwayson

if [[ ! -e /etc/alwayson/alwayson.conf ]]; then
    install -m 0600 "$source_dir/alwayson.conf" /etc/alwayson/alwayson.conf
fi
if [[ ! -e /etc/alwayson/anthropic_api_key ]]; then
    install -m 0600 /dev/null /etc/alwayson/anthropic_api_key
fi

if [[ ! -e /etc/alwayson/github.conf ]]; then
    install -m 0600 "$source_dir/github.conf" /etc/alwayson/github.conf
fi
if [[ ! -e /etc/alwayson/github_token ]]; then
    install -m 0600 /dev/null /etc/alwayson/github_token
fi

/usr/local/bin/alwayson init-db
systemctl daemon-reload
echo '安装完成。用 sudoedit /etc/alwayson/anthropic_api_key 填写密钥，再运行 sudo systemctl enable --now alwayson。'
