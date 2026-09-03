from diskless.bootd.systemd_sockets import FIRST_SYSTEMD_FD, systemd_fds_by_name


def test_no_env_var_returns_empty_dict(monkeypatch):
    monkeypatch.delenv("LISTEN_FDNAMES", raising=False)
    assert systemd_fds_by_name() == {}


def test_empty_env_var_returns_empty_dict(monkeypatch):
    monkeypatch.setenv("LISTEN_FDNAMES", "")
    assert systemd_fds_by_name() == {}


def test_single_name_maps_to_first_fd(monkeypatch):
    monkeypatch.setenv("LISTEN_FDNAMES", "tftp")
    assert systemd_fds_by_name() == {"tftp": FIRST_SYSTEMD_FD}


def test_multiple_names_map_in_order(monkeypatch):
    monkeypatch.setenv("LISTEN_FDNAMES", "tftp:dhcp")
    assert systemd_fds_by_name() == {
        "tftp": FIRST_SYSTEMD_FD,
        "dhcp": FIRST_SYSTEMD_FD + 1,
    }
