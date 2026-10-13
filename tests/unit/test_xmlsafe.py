import pytest

from rootflake.sandbox.xmlsafe import RefusedXml, new_parser


@pytest.mark.parametrize(
    "handler",
    [
        "StartDoctypeDeclHandler",
        "EntityDeclHandler",
        "UnparsedEntityDeclHandler",
        "ExternalEntityRefHandler",
    ],
)
def test_every_dtd_handler_refuses(handler):
    with pytest.raises(RefusedXml):
        getattr(new_parser(), handler)("a", "b", "c", "d")


def test_a_bare_doctype_is_refused():
    with pytest.raises(RefusedXml):
        new_parser().Parse(b'<?xml version="1.0"?><!DOCTYPE project><project/>', True)


def test_plain_xml_parses():
    new_parser().Parse(b"<project><a>1</a></project>", True)
