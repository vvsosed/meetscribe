def test_package_imports():
    import meetscribe

    assert meetscribe is not None


def test_console_entry_point_is_importable():
    from meetscribe.cli import main

    assert callable(main)
