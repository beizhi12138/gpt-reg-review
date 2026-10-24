"""Exercise the mail wait/resend boundary without contacting the auth service."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import auth_flow


@pytest.mark.parametrize("configured,expected", [
    (None, 90), ("90", 90), ("240", 240), ("invalid", 90), ("0", 60),
])
def test_email_wait_default_and_override(monkeypatch, configured, expected):
    monkeypatch.delenv("OTP_TIMEOUT", raising=False)
    flow = auth_flow.AuthFlow.__new__(auth_flow.AuthFlow)
    flow._env_overrides = {} if configured is None else {"OTP_TIMEOUT": configured}
    assert flow._email_otp_timeout() == expected


@pytest.mark.parametrize("is_new,mode", [(True, ""), (False, "passwordless_signup")])
def test_registration_resends_after_90_seconds_and_advances_mail_cutoff(
    monkeypatch, is_new, mode,
):
    # Per-task options must win over an old process-wide value.
    monkeypatch.setenv("OTP_TIMEOUT", "180")
    clock = [1000.0]
    monkeypatch.setattr(auth_flow.time, "time", lambda: clock[0])
    flow = auth_flow.AuthFlow.__new__(auth_flow.AuthFlow)
    flow.result = auth_flow.AuthResult()
    flow._env_overrides = {"OTP_TIMEOUT": "90"}
    flow._existing_email_verification_mode = mode
    for name in ("check_proxy", "warmup", "get_csrf_token", "get_auth_url",
                 "auth_oauth_init", "get_sentinel_token", "register_password"):
        setattr(flow, name, Mock(return_value=True))
    flow.signup = Mock(return_value=is_new)
    events = []
    flow.send_otp = Mock(side_effect=lambda: events.append(("send", clock[0])))
    flow._kickoff_new_registration_otp = Mock(
        side_effect=lambda: events.append(("resend", clock[0])) or True,
    )
    flow.verify_otp = Mock()
    flow.fetch_client_auth_session_dump = Mock()

    class Verified(Exception):
        pass

    flow.create_account = Mock(side_effect=Verified)
    flow._is_registration_disallowed_error = Mock(return_value=False)
    waits = []

    def wait_for_otp(email, *, timeout, issued_after):
        waits.append((timeout, issued_after))
        if len(waits) == 1:
            clock[0] += timeout
            raise TimeoutError("fixture mail did not arrive")
        return "654321"

    mail = SimpleNamespace(
        kind="icloud_relay", pooled=True,
        create_mailbox=lambda: "otp-fixture@example.com",
        wait_for_otp=wait_for_otp,
    )
    with pytest.raises(Verified):
        flow.run_register(mail)

    assert waits == [(90, 1000.0), (90, 1090.0)]
    assert events == [("send", 1000.0), ("resend", 1090.0)]
    flow.verify_otp.assert_called_once_with("654321")
