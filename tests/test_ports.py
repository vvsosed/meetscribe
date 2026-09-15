from meetscribe.ports import LinkResult


def test_link_result_distinguishes_duplicate_from_failure():
    # pw-link reporting "File exists" means the pair is already connected,
    # which is benign. A bool would erase that distinction.
    assert LinkResult.ALREADY_LINKED is not LinkResult.FAILED
    assert LinkResult.ALREADY_LINKED is not LinkResult.LINKED


def test_fakes_satisfy_the_protocols(fake_launcher, fake_linker, fake_clock):
    from meetscribe.ports import Clock, Linker, ProcessLauncher

    assert isinstance(fake_launcher, ProcessLauncher)
    assert isinstance(fake_linker, Linker)
    assert isinstance(fake_clock, Clock)


def test_fake_clock_advances_on_sleep(fake_clock):
    assert fake_clock.monotonic() == 0.0

    fake_clock.sleep(2.0)
    fake_clock.sleep(2.0)

    assert fake_clock.monotonic() == 4.0
    assert fake_clock.slept == [2.0, 2.0]
