"""Regression corpus for credential redaction boundaries."""

import pytest

from grokbuild.redact import redact, redact_text


DUMMY = "S3CR3T_DUMMY_VALUE_1"
PASSWORD_ASSIGNMENT = "password" + "="


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer " + DUMMY,
        "bearer\t" + DUMMY + "\nnext",
        "Bearer split_\nvalue_123",
        "access_token=" + DUMMY,
        "client_secret: " + DUMMY,
        "x-api-key: " + DUMMY,
        "AWS_SECRET_ACCESS_KEY=" + DUMMY,
        '"password": "' + DUMMY + '"',
        r"\"password\":\"" + DUMMY + r"\"",
        "db_password='one two,three'",
        PASSWORD_ASSIGNMENT + DUMMY + ",tail",
        "token" + "=" + "split_\nvalue_123",
        PASSWORD_ASSIGNMENT + '"line one\nline two"',
        "jwt=eyJ" + "A" * 12 + ".payload.signature",
        "aws=" + "A" * 40,
        "google=AIza" + "A" * 35,
        "live=sk_live_" + "A" * 12,
        "app=xapp-" + "A" * 12,
        "pat=glpat-" + "A" * 12,
        "send=SG." + "A" * 22,
        "hug=hf_" + "A" * 12,
        "node=npm_" + "A" * 12,
        "age=AGE-SECRET-KEY-1" + "A" * 12,
        "postgres://user:" + DUMMY + "/part@db.sample/x",
        "redis://user:" + DUMMY + "@cache.sample",
        "mongodb+srv://user:" + DUMMY + "@cluster.sample/db",
        "amqp://user:" + DUMMY + "@queue.sample/vhost",
        "/download?private_token=" + DUMMY + "&page=2",
        "/download?sig=" + DUMMY,
        "/download?X-Amz-Signature=" + DUMMY,
        "/download?access_token=" + DUMMY,
        "-----  BEGIN" + "   PRIVATE KEY-----\ndata",
        "-----begin private key-----\ndata without end",
        "-----BEGIN " + "PRIVATE KEY-----\ndata\n-----END " + "PRIVATE KEY-----",
        "Bearer soft_\nwrapped_123",
        "\u0440\u0430ssword=" + DUMMY,
        "\u0410KIAABCDEFGHIJKLMNOP",
    ],
)
def test_breach_corpus(text):
    output = redact_text(text)
    assert "[redacted]" in output
    assert DUMMY not in output
    if text == PASSWORD_ASSIGNMENT + DUMMY + ",tail":
        assert ",tail" not in output


@pytest.mark.parametrize(
    "text",
    [
        "https://sample.test/path",
        "?page=2&q=abc",
        "rotate your keys regularly",
        "sort_key=price",
        "arn:aws:iam::123456789012:role/console",
        "-----BEGIN PUBLIC KEY-----\ndata\n-----END PUBLIC KEY-----",
        "short id 0123456789abcdef",
        "base64 image AAAABBBBCCCCDDDDEEEE",
        "tokenize keyboard secretariat",
    ],
)
def test_clean_corpus_survives(text):
    assert redact_text(text) == text


def test_structural_compound_keys_and_nesting():
    value = {
        "client_secret": "dummy",
        "aws_secret_access_key": "dummy",
        "db_password": "dummy",
        "passwords": ["dummy", {"nested_client_secret": "dummy"}],
        "name": "survives",
    }
    output = redact(value)
    assert output["client_secret"] == "[redacted]"
    assert output["aws_secret_access_key"] == "[redacted]"
    assert output["db_password"] == "[redacted]"
    assert output["passwords"] == "[redacted]"
    assert output["name"] == "survives"


@pytest.mark.parametrize("text", ["password=" + DUMMY, "Bearer " + DUMMY, "?token=" + DUMMY])
def test_redaction_is_idempotent(text):
    assert redact_text(redact_text(text)) == redact_text(text)
