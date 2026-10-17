from auto_zettelkasten.cli import main


def test_serve_launches_local_app_without_mapping(monkeypatch):
    calls = []
    monkeypatch.setattr("auto_zettelkasten.webapp.serve", lambda **kwargs: calls.append(kwargs))
    assert main(["serve", "--port", "9876", "--no-browser"]) == 0
    assert calls == [{"port": 9876, "open_browser": False}]


def test_serve_rejects_invalid_port(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("auto_zettelkasten.webapp.serve", lambda **kwargs: calls.append(kwargs))
    assert main(["serve", "--port", "65536"]) == 2
    assert not calls
    assert "port must be between" in capsys.readouterr().err
