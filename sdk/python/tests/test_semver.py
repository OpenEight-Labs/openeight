from openeight._semver import is_semver


def test_semver_grammar_and_bounds():
    for value in ("1.0.0", "1.2.3-alpha.1+build.01", "18446744073709551615.0.0"):
        assert is_semver(value)
    for value in ("v1.0.0", "01.0.0", "1.0.0-01", "1.2", "1.0.0\n", "18446744073709551616.0.0"):
        assert not is_semver(value)
