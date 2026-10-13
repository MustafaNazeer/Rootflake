"""An expat parser for untrusted XML that refuses any DTD inside the parser itself.

Refusing the DOCTYPE and every entity declaration in expat's own handlers works whatever
the document's encoding, which a byte level check does not.
"""

from xml.parsers import expat


class RefusedXml(ValueError):
    pass


def _refuse(*_args: object) -> None:
    raise RefusedXml("DTDs and entity declarations are not accepted")


def new_parser() -> expat.XMLParserType:
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    parser.UnparsedEntityDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse
    return parser
