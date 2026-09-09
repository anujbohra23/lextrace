"""Minimal command-line entry point."""

import argparse


def main() -> None:
    """Display CLI help until research commands are implemented."""
    parser = argparse.ArgumentParser(description="LexTrace research tools")
    parser.parse_args()
    parser.print_help()
