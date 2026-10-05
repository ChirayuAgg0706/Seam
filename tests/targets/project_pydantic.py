"""Target of the real-project scenarios: pydantic-core (PyO3). python project_pydantic.py [error]"""
import sys

from pydantic_core import SchemaValidator, core_schema


def double(value):
    return value * 2  # double-body


def main(mode):
    validator = SchemaValidator(core_schema.int_schema(gt=0))  # build
    number = validator.validate_python("42")  # validate
    doubling = SchemaValidator(core_schema.no_info_plain_validator_function(double))
    doubled = doubling.validate_python(21)  # callback
    print("number", number, "doubled", doubled)  # print
    if mode == "error":
        validator.validate_python(-1)  # error


main(sys.argv[1] if len(sys.argv) > 1 else "")  # module-main
