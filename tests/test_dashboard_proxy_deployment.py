from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT_ROOT / "deployment" / "Install-DashboardProxy.sh"
TEMPLATE = PROJECT_ROOT / "deployment" / "nginx" / "bongus-dashboard.conf.in"
DEPLOYMENT_README = PROJECT_ROOT / "deployment" / "README.md"


def test_dashboard_proxy_asset_keeps_uvicorn_private_and_nginx_tls_only() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    rendered = (
        template.replace("@PUBLIC_IP@", "66.42.45.59")
        .replace("@CERT_FILE@", "/etc/ssl/bongus-dashboard/dashboard.crt")
        .replace("@KEY_FILE@", "/etc/ssl/bongus-dashboard/dashboard.key")
    )

    assert "@PUBLIC_IP@" not in rendered
    assert "@CERT_FILE@" not in rendered
    assert "@KEY_FILE@" not in rendered
    assert "listen 80;" in rendered
    assert "listen 443 ssl;" in rendered
    assert "return 301 https://66.42.45.59$request_uri;" in rendered
    assert "proxy_pass http://127.0.0.1:8080;" in rendered
    assert "proxy_set_header Upgrade $http_upgrade;" in rendered
    assert "proxy_set_header Connection $bongus_connection_upgrade;" in rendered
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in rendered
    assert "Strict-Transport-Security" not in rendered
    assert not re.search(r"\blisten\s+[^;]*8080\b", rendered)
    assert "proxy_pass http://0.0.0.0" not in rendered


def test_dashboard_proxy_installer_is_fail_closed_and_does_not_mutate_firewall() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    install_proxy_body = installer.split("install_proxy() {", 1)[1].split(
        "\n}\n\nprint_result()", 1
    )[0]

    assert "set -Eeuo pipefail" in installer
    assert '[[ "$(id -u)" == "0" ]]' in installer
    assert "require_command nginx" in installer
    assert "require_command openssl" in installer
    assert "require_command ss" in installer
    assert "validate_public_ipv4" in installer
    assert "assert_loopback_backend" in installer
    assert 'listeners="$(ss -H -ltn)" || die' in installer
    assert "port 8080 is listening outside loopback" in installer
    assert "subjectAltName=IP:$PUBLIC_IP" in installer
    assert 'openssl x509 -in "$cert_file" -noout -checkip "$PUBLIC_IP"' in installer
    assert 'grep -Fq "IP Address:$PUBLIC_IP"' not in installer
    assert "-fingerprint -sha256" in installer
    assert "certificate_valid_for_ip" in installer
    assert "Rolling back dashboard proxy files" in installer
    assert "BACKUP_ROOT=\"/var/backups/bongus-dashboard-proxy\"" in installer
    assert install_proxy_body.index("nginx -t") < install_proxy_body.index("reload_nginx")

    forbidden_mutations = ("apt-get ", "apt ", "dnf ", "yum ", "ufw ", "iptables ", "nft ")
    assert all(token not in installer for token in forbidden_mutations)
    assert "--host 0.0.0.0" not in installer
    assert "listen 8080" not in installer


def test_dashboard_proxy_runbook_documents_trust_firewall_and_rollback() -> None:
    readme = DEPLOYMENT_README.read_text(encoding="utf-8")

    assert "Install-DashboardProxy.sh" in readme
    assert "browser trust warning" in readme
    assert "DNS name" in readme and "ACME" in readme
    assert "SHA-256 fingerprint" in readme
    assert "sudo ufw allow 80/tcp" in readme
    assert "sudo ufw allow 443/tcp" in readme
    assert "sudo ufw delete allow 8080/tcp" in readme
    assert "/var/backups/bongus-dashboard-proxy" in readme
    assert "127.0.0.1:8080" in readme
    assert "--write-out 'HTTP %{http_code}" in readme
    assert "curl --insecure --head" not in readme


@pytest.mark.skipif(
    shutil.which("bash") is None or os.name == "nt",
    reason="native Bash path semantics unavailable",
)
def test_dashboard_proxy_shell_has_valid_syntax() -> None:
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-n", str(INSTALLER)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    shutil.which("bash") is None or os.name == "nt",
    reason="native Bash path semantics unavailable",
)
@pytest.mark.parametrize("address", ["66.42.45.59", "1.1.1.1", "8.8.8.8"])
def test_public_ipv4_validator_accepts_public_addresses(address: str) -> None:
    result = subprocess.run(
        [
            shutil.which("bash") or "bash",
            "-c",
            'source "$1"; validate_public_ipv4 "$2"',
            "bash",
            str(INSTALLER),
            address,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    shutil.which("bash") is None or os.name == "nt",
    reason="native Bash path semantics unavailable",
)
@pytest.mark.parametrize(
    "address",
    [
        "",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.1.1",
        "172.16.0.1",
        "192.0.2.1",
        "192.168.1.1",
        "198.18.0.1",
        "198.51.100.1",
        "203.0.113.1",
        "224.0.0.1",
        "256.1.1.1",
        "not-an-ip",
    ],
)
def test_public_ipv4_validator_rejects_non_public_addresses(address: str) -> None:
    result = subprocess.run(
        [
            shutil.which("bash") or "bash",
            "-c",
            'source "$1"; ! validate_public_ipv4 "$2"',
            "bash",
            str(INSTALLER),
            address,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
