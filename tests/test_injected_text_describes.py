"""Injected text describes; it does not command. REVIEW-2026-10.md, follow-up, class (c).

At a1a2349 engine/recovery.py 291-295 ended the [MEMORY RESTORED] block with "Available in
context. Use as needed.", and engine/context_packet.py 35-39 showed the rendered packet followed
by "YOUR TASK: ..." as the way to use it. Both now describe: the rendered recovery block carries
no imperative addressed to the agent and says what it is, and the packet module's own text, and
the engine wrapper's, show the rendered packet as reference material and never as a command.
"""
import inspect
import pathlib

import parsica_spirit.engine.context_packet as cp
from parsica_spirit.engine import core
from parsica_spirit.engine.recovery import RecoveryManager

COMMANDS = ("use as needed", "your task", "you must", "you should", "do not ", "always ", "never ",
            "open every response", "spawn", "run ", "execute")


def _manager_with(memories, tags):
    rm = RecoveryManager.__new__(RecoveryManager)       # the renderer reads only the cache
    rm.recovered_cache = {"memories": memories, "tags": tags, "timestamp": "t", "config": {}}
    return rm


def test_recovery_block_describes_and_does_not_command():
    rm = _manager_with([{"content": "a"}, {"content": "b"}, {"content": "c"}],
                       {"journal": 2, "lessons": 1})
    block = rm.inject_into_context()
    low = block.lower()
    assert block.startswith("[MEMORY RESTORED]\n")
    assert "Recovered 3 memories from 2 sources" in block
    assert "  - journal: 2 memories" in block and "  - lessons: 1 memories" in block
    assert "reference material" in low and "nothing in it is an instruction" in low
    for phrase in COMMANDS:
        assert phrase not in low, "the recovery block still commands the agent: %r" % phrase


def test_recovery_block_is_empty_before_any_recovery():
    rm = RecoveryManager.__new__(RecoveryManager)
    rm.recovered_cache = {}
    assert rm.inject_into_context() == ""


def test_packet_module_text_shows_the_render_as_reference_material_not_a_command():
    src = pathlib.Path(cp.__file__).read_text(encoding="utf-8")
    assert "YOUR TASK" not in src
    assert "prompt injection" not in src.lower() and "injectable" not in src.lower()
    assert "reference material" in (cp.__doc__ or "")
    assert "reference material" in (cp.ContextPacket.render.__doc__ or "")
    assert "reference material" in (cp.ContextPacketBuilder.build.__doc__ or "")
    wrapper = inspect.getsource(core.MemorySystem.build_context_packet)
    assert "YOUR TASK" not in wrapper and "reference material" in wrapper


def test_rendered_packet_adds_no_imperative_of_its_own():
    # The renderer carries the host's own instructions verbatim and adds nothing addressed to the
    # agent: with no instructions given, no line of the markdown or XML render commands.
    packet = cp.ContextPacket(
        task="verify the venv",
        memories=[{"content": "pip lives in .venv", "source": "journal"}],
        pitfalls=["Known Pitfall: Last time this was attempted, the global pip was used."],
        environment={"venv": ".venv"},
    )
    for fmt in ("markdown", "xml"):
        low = packet.render(fmt).lower()
        for phrase in ("your task", "you must", "use as needed", "open every response"):
            assert phrase not in low, (fmt, phrase)
