"""``hermes update`` never touches upstream unless the caller asks for it (``--upstream``).

Downstream users of a fork must get the fork's own line only: integrating upstream/main is the fork
maintainer's job, so the default run must not compare, prompt, or merge it.
"""

import argparse

from hermes_cli.subcommands.update import build_update_parser


def _parser():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_update_parser(sub, cmd_update=lambda args: None)
    return parser


def test_upstream_is_opt_in():
    args = _parser().parse_args(["update"])
    assert args.upstream is False


def test_the_flag_is_accepted():
    args = _parser().parse_args(["update", "--upstream"])
    assert args.upstream is True
