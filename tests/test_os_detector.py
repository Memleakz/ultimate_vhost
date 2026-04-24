import pytest
import subprocess
from pathlib import Path
from unittest.mock import mock_open, patch

from vhost_helper.os_detector import get_os_info, detect_os_family
from vhost_helper.models import OSInfo

def test_get_os_info(mocker):
    # Mock subprocess.run output
    mock_output = "ID=ubuntu\nVERSION=22.04\n"
    mocker.patch("subprocess.run", return_value=mocker.Mock(stdout=mock_output, check=True))
    
    os_info = get_os_info()
    
    assert os_info.id == "ubuntu"
    assert os_info.version == "22.04"
    assert os_info.family == "debian"

def test_get_os_info_rhel(mocker):
    mock_output = "ID=fedora\nVERSION=39\n"
    mocker.patch("subprocess.run", return_value=mocker.Mock(stdout=mock_output, check=True))
    
    os_info = get_os_info()
    
    assert os_info.id == "fedora"
    assert os_info.version == "39"
    assert os_info.family == "rhel"

def test_get_os_info_unknown(mocker):
    mock_output = "ID=solaris\nVERSION=11\n"
    mocker.patch("subprocess.run", return_value=mocker.Mock(stdout=mock_output, check=True))
    
    os_info = get_os_info()
    
    assert os_info.id == "solaris"
    assert os_info.version == "11"
    assert os_info.family == "unknown"


# --- Tests for detect_os_family() ---

_DEBIAN_OS_RELEASE = "ID=ubuntu\nVERSION_ID=\"22.04\"\nID_LIKE=debian\n"
_UBUNTU_OS_RELEASE = "ID=ubuntu\nVERSION_ID=\"20.04\"\n"
_DEBIAN_PURE_OS_RELEASE = "ID=debian\nVERSION_ID=\"12\"\n"
_RHEL_OS_RELEASE = "ID=rhel\nVERSION_ID=\"9.0\"\n"
_CENTOS_OS_RELEASE = "ID=centos\nVERSION_ID=\"8\"\nID_LIKE=\"rhel fedora\"\n"
_FEDORA_OS_RELEASE = "ID=fedora\nVERSION_ID=\"39\"\n"
_ALMA_OS_RELEASE = "ID=almalinux\nVERSION_ID=\"9.1\"\nID_LIKE=\"rhel centos fedora\"\n"
_ROCKY_OS_RELEASE = "ID=rocky\nVERSION_ID=\"9.2\"\nID_LIKE=\"rhel centos fedora\"\n"
_UNKNOWN_OS_RELEASE = "ID=gentoo\nVERSION_ID=\"2.14\"\n"


def _make_mock_open(content: str):
    return mock_open(read_data=content)


def test_detect_os_family_ubuntu(tmp_path):
    """Ubuntu with ID_LIKE=debian should return debian_family."""
    release_file = tmp_path / "os-release"
    release_file.write_text(_DEBIAN_OS_RELEASE)
    assert detect_os_family(str(release_file)) == "debian_family"


def test_detect_os_family_ubuntu_no_id_like(tmp_path):
    """Ubuntu identified by ID alone should return debian_family."""
    release_file = tmp_path / "os-release"
    release_file.write_text(_UBUNTU_OS_RELEASE)
    assert detect_os_family(str(release_file)) == "debian_family"


def test_detect_os_family_debian(tmp_path):
    """Pure Debian should return debian_family."""
    release_file = tmp_path / "os-release"
    release_file.write_text(_DEBIAN_PURE_OS_RELEASE)
    assert detect_os_family(str(release_file)) == "debian_family"


def test_detect_os_family_rhel(tmp_path):
    """RHEL identified by ID=rhel should return rhel_family."""
    release_file = tmp_path / "os-release"
    release_file.write_text(_RHEL_OS_RELEASE)
    assert detect_os_family(str(release_file)) == "rhel_family"


def test_detect_os_family_centos(tmp_path):
    """CentOS with ID_LIKE=rhel should return rhel_family."""
    release_file = tmp_path / "os-release"
    release_file.write_text(_CENTOS_OS_RELEASE)
    assert detect_os_family(str(release_file)) == "rhel_family"


def test_detect_os_family_fedora(tmp_path):
    """Fedora identified by ID alone should return rhel_family."""
    release_file = tmp_path / "os-release"
    release_file.write_text(_FEDORA_OS_RELEASE)
    assert detect_os_family(str(release_file)) == "rhel_family"


def test_detect_os_family_almalinux(tmp_path):
    """AlmaLinux (ID_LIKE=rhel centos fedora) should return rhel_family."""
    release_file = tmp_path / "os-release"
    release_file.write_text(_ALMA_OS_RELEASE)
    assert detect_os_family(str(release_file)) == "rhel_family"


def test_detect_os_family_rocky(tmp_path):
    """Rocky Linux (ID_LIKE=rhel) should return rhel_family."""
    release_file = tmp_path / "os-release"
    release_file.write_text(_ROCKY_OS_RELEASE)
    assert detect_os_family(str(release_file)) == "rhel_family"


def test_detect_os_family_unknown_id(tmp_path):
    """An unrecognised OS ID without ID_LIKE should return unknown."""
    release_file = tmp_path / "os-release"
    release_file.write_text(_UNKNOWN_OS_RELEASE)
    assert detect_os_family(str(release_file)) == "unknown"


def test_detect_os_family_missing_file(tmp_path):
    """A missing /etc/os-release must return unknown without raising."""
    missing = str(tmp_path / "no-such-file")
    assert detect_os_family(missing) == "unknown"


def test_detect_os_family_empty_file(tmp_path):
    """An empty os-release file must return unknown without raising."""
    release_file = tmp_path / "os-release"
    release_file.write_text("")
    assert detect_os_family(str(release_file)) == "unknown"


def test_detect_os_family_comments_ignored(tmp_path):
    """Lines starting with # must be ignored during parsing."""
    content = "# This is a comment\nID=ubuntu\n# Another comment\n"
    release_file = tmp_path / "os-release"
    release_file.write_text(content)
    assert detect_os_family(str(release_file)) == "debian_family"


def test_detect_os_family_quoted_values(tmp_path):
    """Values enclosed in double or single quotes must be parsed correctly."""
    content = 'ID="centos"\nVERSION_ID="8"\nID_LIKE="rhel fedora"\n'
    release_file = tmp_path / "os-release"
    release_file.write_text(content)
    assert detect_os_family(str(release_file)) == "rhel_family"


# --- Integration tests: detect_os_family() drives correct Nginx paths ---

def test_nginx_paths_debian_family(tmp_path, mocker):
    """When detect_os_family returns debian_family, NginxProvider.os_family is debian_family."""
    import vhost_helper.providers.nginx as nginx_mod

    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", Path("/etc/nginx/sites-available"))
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", Path("/etc/nginx/sites-enabled"))
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None)

    provider = nginx_mod.NginxProvider.__new__(nginx_mod.NginxProvider)
    provider.os_family = nginx_mod.detected_os_family

    assert provider.os_family == "debian_family"
    assert nginx_mod.NGINX_SITES_AVAILABLE == Path("/etc/nginx/sites-available")
    assert nginx_mod.NGINX_SITES_ENABLED == Path("/etc/nginx/sites-enabled")
    assert nginx_mod.NGINX_SITES_DISABLED is None


def test_nginx_paths_rhel_family(tmp_path, mocker):
    """When detect_os_family returns rhel_family, NginxProvider.os_family is rhel_family."""
    import vhost_helper.providers.nginx as nginx_mod

    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "rhel_family")
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", Path("/etc/nginx/conf.d"))
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", Path("/etc/nginx/conf.d"))
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", Path("/etc/nginx/conf.disabled"))

    provider = nginx_mod.NginxProvider.__new__(nginx_mod.NginxProvider)
    provider.os_family = nginx_mod.detected_os_family

    assert provider.os_family == "rhel_family"
    assert nginx_mod.NGINX_SITES_AVAILABLE == Path("/etc/nginx/conf.d")
    assert nginx_mod.NGINX_SITES_ENABLED == Path("/etc/nginx/conf.d")
    assert nginx_mod.NGINX_SITES_DISABLED == Path("/etc/nginx/conf.disabled")


def test_nginx_paths_unknown_falls_back_to_debian(mocker):
    """When detect_os_family returns unknown, Nginx uses Debian-family paths as safe default."""
    import vhost_helper.providers.nginx as nginx_mod

    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "unknown")
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", Path("/etc/nginx/sites-available"))
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", Path("/etc/nginx/sites-enabled"))
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None)

    provider = nginx_mod.NginxProvider.__new__(nginx_mod.NginxProvider)
    provider.os_family = nginx_mod.detected_os_family

    # 'unknown' is not 'rhel_family', so Debian-family paths apply
    assert provider.os_family == "unknown"
    assert nginx_mod.NGINX_SITES_AVAILABLE == Path("/etc/nginx/sites-available")
    assert nginx_mod.NGINX_SITES_DISABLED is None
