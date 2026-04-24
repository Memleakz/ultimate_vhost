import pytest
import os
import tempfile
import threading
from pathlib import Path
from vhost_helper.models import VHostConfig, ServerType
from vhost_helper.providers.nginx import NginxProvider
from vhost_helper.hostfile import add_entry
from typer.testing import CliRunner
from vhost_helper.main import app

runner = CliRunner()

def test_large_number_of_vhosts(mocker):
    """Performance/Memory: Simulate creating 100 vhosts."""
    mocker.patch("vhost_helper.providers.nginx.get_sudo_prefix", return_value=[])
    mocker.patch("vhost_helper.providers.nginx.run_elevated_command")
    mocker.patch("vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False)
    mocker.patch.object(NginxProvider, "validate_config", return_value=True)
    mocker.patch.object(NginxProvider, "reload")
    
    provider = NginxProvider()
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", tmp_path)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", tmp_path)
        
        for i in range(100):
            config = VHostConfig(
                domain=f"site{i}.test",
                document_root=tmp_path,
                port=80,
                server_type=ServerType.NGINX
            )
            provider.create_vhost(config)

def test_concurrent_vhost_creation(mocker):
    """Concurrency: Simulate concurrent vhost creation attempts."""
    # This is tricky to test truly concurrently due to global mocks,
    # but we can try to see if any shared state causes issues.
    mocker.patch("vhost_helper.providers.nginx.get_sudo_prefix", return_value=[])
    mocker.patch("vhost_helper.providers.nginx.run_elevated_command")
    mocker.patch("vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False)
    mocker.patch.object(NginxProvider, "validate_config", return_value=True)
    mocker.patch.object(NginxProvider, "reload")
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", tmp_path)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", tmp_path)
        
        def create_site(i):
            provider = NginxProvider()
            config = VHostConfig(
                domain=f"concurrent{i}.test",
                document_root=tmp_path,
                port=80,
                server_type=ServerType.NGINX
            )
            provider.create_vhost(config)
            
        threads = []
        for i in range(10):
            t = threading.Thread(target=create_site, args=(i,))
            threads.append(t)
            t.start()
            
        for t in threads:
            t.join()

def test_domain_path_traversal_advanced():
    """Security: Test advanced path traversal attempts in domain name."""
    invalid_domains = [
        ("../../etc/passwd", "Domain name cannot contain double dots"),
        ("site.test/../../etc/shadow", "Domain name cannot contain double dots"),
        ("site.test\\..\\..\\windows\\system32\\config\\sam", "Domain name cannot contain double dots"),
        ("~/.ssh/id_rsa", "Invalid domain format"),
        ("site.test%2e%2e%2f", "Invalid domain format"),
        ("..%2f..%2f", "Domain name cannot contain double dots"),
    ]
    
    for domain, expected_msg in invalid_domains:
        result = runner.invoke(app, ["create", domain, "/tmp"])
        assert result.exit_code != 0
        assert expected_msg in result.stdout

def test_requirements_file_content():
    """QA: Verify requirements.txt exists and has expected content."""
    req_path = Path(__file__).resolve().parent.parent / "requirements.txt"
    assert req_path.exists()
    content = req_path.read_text()
    assert "typer" in content.lower()
    assert "pydantic" in content.lower()
    assert "jinja2" in content.lower()
    assert "rich" in content.lower()

def test_os_detector_invalid_input_types():
    """Edge Case: Passing invalid types to models (though Pydantic handles this)."""
    from vhost_helper.models import VHostConfig, OSInfo
    
    with pytest.raises(Exception): # Pydantic ValidationError
        VHostConfig(domain=123, document_root="/tmp", port="eighty")

    with pytest.raises(Exception):
        OSInfo(id=None, version=True, family=1.0)
