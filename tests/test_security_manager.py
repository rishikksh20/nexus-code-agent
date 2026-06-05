from __future__ import annotations

from nexus.security import ApprovalManager, ApprovalPolicy, ApprovalScope


def test_once_approval_is_consumed_per_invocation_signature():
    manager = ApprovalManager(policy=ApprovalPolicy.ON_REQUEST)
    args = {"path": "calculator.py", "content": "print('hi')\n"}

    manager.record_approval("write_file", ApprovalScope.ONCE, arguments=args)

    assert manager.is_pre_approved("write_file", args)
    assert not manager.is_pre_approved(
        "write_file",
        {"path": "logging_calculator.py", "content": "print('hi')\n"},
    )

    manager.consume_approval("write_file", arguments=args)

    assert not manager.is_pre_approved("write_file", args)


def test_turn_approval_is_signature_scoped_and_cleared_next_turn():
    manager = ApprovalManager(policy=ApprovalPolicy.APPROVE_TURN)
    approved_args = {"command": "mkdir build"}

    manager.record_approval("bash", ApprovalScope.TURN, arguments=approved_args)

    assert manager.is_pre_approved("bash", approved_args)
    assert not manager.is_pre_approved("bash", {"command": "mkdir dist"})

    manager.begin_turn()

    assert not manager.is_pre_approved("bash", approved_args)


def test_session_approval_is_signature_scoped_across_turns():
    manager = ApprovalManager(policy=ApprovalPolicy.APPROVE_SESSION)
    approved_args = {"path": ".env"}

    manager.record_approval("read_file", ApprovalScope.SESSION, arguments=approved_args)
    manager.begin_turn()

    assert manager.is_pre_approved("read_file", approved_args)
    assert not manager.is_pre_approved("read_file", {"path": ".gitignore"})


def test_turn_wide_approval_skips_all_prompts_for_current_turn():
    manager = ApprovalManager(policy=ApprovalPolicy.ON_REQUEST)

    manager.record_turn_wide_mutating_approval()

    assert manager.is_turn_wide_mutating_preapproved("write_file", is_mutating=True)
    assert manager.is_turn_wide_mutating_preapproved("get_time", is_mutating=False)
    assert manager.is_turn_wide_mutating_preapproved("bash", is_mutating=True, risk_level="medium")
    assert manager.is_turn_wide_mutating_preapproved("bash", is_mutating=True, risk_level="dangerous")

