from meetscribe.graph import PwGraph
from meetscribe.ports import LinkResult
from tests.conftest import FakeGraphSource


def test_link_result_distinguishes_duplicate_from_failure():
    # pw-link reporting "File exists" means the pair is already connected,
    # which is benign. A bool would erase that distinction.
    assert LinkResult.ALREADY_LINKED is not LinkResult.FAILED
    assert LinkResult.ALREADY_LINKED is not LinkResult.LINKED


def test_fakes_expose_the_protocol_methods(fake_launcher, fake_linker, fake_clock):
    # @runtime_checkable only verifies that the method NAMES exist. It checks
    # neither signatures nor return types, so this pins the shape of the fakes,
    # not their contracts.
    from meetscribe.ports import (
        Clock,
        GraphSource,
        Linker,
        ManagedProcess,
        ProcessLauncher,
    )

    assert isinstance(fake_launcher, ProcessLauncher)
    assert isinstance(fake_linker, Linker)
    assert isinstance(fake_clock, Clock)
    assert isinstance(fake_launcher.spawn(["pw-record"]), ManagedProcess)
    assert isinstance(FakeGraphSource(PwGraph()), GraphSource)


def test_fake_clock_advances_on_sleep(fake_clock):
    assert fake_clock.monotonic() == 0.0

    fake_clock.sleep(2.0)
    fake_clock.sleep(2.0)

    assert fake_clock.monotonic() == 4.0
    assert fake_clock.slept == [2.0, 2.0]
