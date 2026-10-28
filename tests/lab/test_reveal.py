# pyright: basic
"""Reveal: the owner lets the agent see one sealed value, after approving that one value."""

from muse_mcp_adapter.cli import main as cli
from trustlab.owner import OwnerSession

from .test_owner_mode import PASSCODE, held
from .test_sealing import sealed_lab

PHONE = "+1 555 0101"
SEAL_PHONE_AND_EMAIL = [
    {"path": "$.contacts[*].email", "label": "contact.email", "rung": "handle"},
    {"path": "$.contacts[*].phone", "label": "contact.phone", "rung": "handle"},
]
REVEALABLE = {
    "contacts_search": {"release": {"fields": SEAL_PHONE_AND_EMAIL, "reveal": ["contact.phone"]}}
}
NOT_REVEALABLE = {"contacts_search": {"release": {"fields": SEAL_PHONE_AND_EMAIL}}}


def dana(adapter) -> dict:
    return adapter.call("contacts_search", {"query": "dana"}).structured["contacts"][0]


def test_a_sealed_phone_is_a_handle_and_the_agent_can_ask_to_see_it() -> None:
    with sealed_lab(owner=True, extra=REVEALABLE) as (_, adapter):
        card = dana(adapter)
        assert card["phone"].startswith("[contact.phone:") and PHONE not in str(card)
        assert "reveal_value" in [tool.name for tool in adapter.list_tools()]

        asked = adapter.call("reveal_value", {"handle": card["phone"]})
        assert asked.status == 409 and asked.error == "APPROVAL_REQUIRED"
        assert PHONE not in str(asked.raw)  # asking does not show it


def test_the_owner_page_shows_the_real_value_and_approving_releases_it_once() -> None:
    with sealed_lab(owner=True, extra=REVEALABLE) as (_, adapter):
        owner = OwnerSession(PASSCODE)
        handle = dana(adapter)["phone"]
        link = held(adapter.call("reveal_value", {"handle": handle}))

        page = owner.view(link).text
        assert PHONE in page and "contact.phone" in page and "passcode" in page.lower()
        assert owner.decide(link, approve=True).status_code == 200

        shown = adapter.call("reveal_value", {"handle": handle})
        assert shown.ok and shown.structured == {"label": "contact.phone", "value": PHONE}

        again = adapter.call("reveal_value", {"handle": handle})  # spent: ask again
        assert again.status == 409 and again.error == "APPROVAL_REQUIRED"


def test_a_declined_reveal_stays_declined() -> None:
    with sealed_lab(owner=True, extra=REVEALABLE) as (_, adapter):
        owner = OwnerSession(PASSCODE)
        handle = dana(adapter)["phone"]
        owner.decide(held(adapter.call("reveal_value", {"handle": handle})), approve=False)
        for _ in range(2):
            refused = adapter.call("reveal_value", {"handle": handle})
            assert refused.status == 403 and refused.error == "APPROVAL_DENIED"
            assert PHONE not in str(refused.raw)


def test_the_agent_cannot_approve_its_own_request_without_the_passcode() -> None:
    with sealed_lab(owner=True, extra=REVEALABLE) as (_, adapter):
        handle = dana(adapter)["phone"]
        link = held(adapter.call("reveal_value", {"handle": handle}))
        guesser = OwnerSession("not the passcode")
        assert guesser.decide(link, approve=True).status_code == 403
        assert adapter.call("reveal_value", {"handle": handle}).status == 409


def test_a_value_the_owner_did_not_make_revealable_cannot_be_asked_for() -> None:
    with sealed_lab(owner=True, extra=REVEALABLE) as (_, adapter):
        email = dana(adapter)["email"]
        refused = adapter.call("reveal_value", {"handle": email})
        assert refused.status == 403 and refused.error == "HANDLE_NOT_ALLOWED_HERE"
        assert "contact.email" in refused.raw["message"]


def test_without_any_revealable_label_the_operation_does_not_exist() -> None:
    with sealed_lab(owner=True, extra=NOT_REVEALABLE) as (_, adapter):
        assert "reveal_value" not in [tool.name for tool in adapter.list_tools()]
        handle = dana(adapter)["phone"]
        assert adapter.call("reveal_value", {"handle": handle}).status == 404


def test_made_up_or_malformed_requests_are_refused() -> None:
    with sealed_lab(owner=True, extra=REVEALABLE) as (_, adapter):
        invented = adapter.call("reveal_value", {"handle": "[contact.phone:aaaaaaaa]"})
        assert invented.status == 400 and invented.error == "HANDLE_UNKNOWN"
        bad = adapter.call("reveal_value", {"handle": "x", "extra": 1})
        assert bad.status == 400 and bad.error == "REQUEST_VALIDATION_FAILED"
        assert adapter.call("reveal_value", {}).status == 400


def test_a_handle_from_one_connection_is_not_revealable_on_another() -> None:
    with sealed_lab(owner=True, extra=REVEALABLE) as (_, adapter):
        handle = dana(adapter)["phone"]
        # a made up handle with the right shape but for a value this connection never held
        other = handle[:-2] + ("aa" if not handle.endswith("aa]") else "bb") + "]"
        assert adapter.call("reveal_value", {"handle": other}).error == "HANDLE_UNKNOWN"


def test_the_record_counts_a_reveal_and_never_holds_the_value(tmp_path, capsys) -> None:
    with sealed_lab(owner=True, extra=REVEALABLE, directory=tmp_path) as (_, adapter):
        owner = OwnerSession(PASSCODE)
        handle = dana(adapter)["phone"]
        owner.decide(held(adapter.call("reveal_value", {"handle": handle})), approve=True)
        assert adapter.call("reveal_value", {"handle": handle}).ok

        assert cli(["--database", str(tmp_path / "adapter.db"), "activity"]) == 0
        digest = capsys.readouterr().out
        assert "revealed" in digest and PHONE not in digest
        assert cli(["--database", str(tmp_path / "adapter.db"), "verify-ledger"]) == 0
