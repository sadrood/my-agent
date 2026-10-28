from strings_utils import slugify


def test_basic():
    assert slugify("Hello World") == "hello-world"


def test_extra_spaces():
    assert slugify("  A  B ") == "a-b"


def test_punctuation():
    assert slugify("Hello, World!") == "hello-world"
