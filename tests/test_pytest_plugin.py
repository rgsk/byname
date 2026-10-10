import pytest

pytest_plugins = ["pytester"]

PYN = """\
def make(*, name: str, age: int):
    return (name=, age=)


def test_passes():
    name, age = "r", 26
    assert make(name=, age=).age == 26


def test_fails():
    (age=) = make(name="r", age=26)
    assert age == 27
"""


def test_pyn_test_files_are_collected_at_their_own_lines(pytester: pytest.Pytester):
    # nodeids and line numbers are the .pyn's (location is 0-based: the defs are on lines 5 and 10),
    # which is what an editor puts its run buttons on
    pytester.makefile(".pyn", test_people=PYN)
    pytester.makefile(".pyn", helpers="def test_not_collected(): ...\n")  # not a test file name
    items, _ = pytester.inline_genitems()
    assert [(i.nodeid, i.location[1]) for i in items] == [
        ("test_people.pyn::test_passes", 4),
        ("test_people.pyn::test_fails", 9),
    ]


def test_failing_asserts_show_their_values_like_py_tests(pytester: pytest.Pytester):
    # asserts are rewritten, so a failure reads `assert 26 == 27`, pointing into the .pyn
    pytester.makefile(".pyn", test_people=PYN)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*assert 26 == 27*", "test_people.pyn:12: AssertionError"])


def test_a_pyn_test_imports_its_neighbours(pytester: pytest.Pytester):
    # the test's folder is on sys.path, and .pyn neighbours import through byname's hook
    pytester.makefile(".pyn", people="def make(*, name: str):\n    return (name=)\n")
    pytester.makefile(
        ".pyn", test_imports="from people import make\n\n\ndef test_it():\n    assert make(name='r').name == 'r'\n"
    )
    pytester.runpytest().assert_outcomes(passed=1)
