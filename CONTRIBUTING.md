# Contributing

Thanks for your interest in contributing!

## Reporting bugs

Open an issue with:
- What you ran (command + flags)
- What happened vs what you expected
- Your OS and Python version

## Adding a printer

Edit the `PRINTERS` dict in `generator.py` with the bed size in mm. Dual-nozzle printers should use the overlap area where both nozzles can reach.

## Adding filament categories

The scraper auto-discovers products from the BambuLab store. If a new category needs special parsing (like TPU hardness detection), edit `parse_variant_name()` in `scraper.py`.

## Pull requests

- Keep changes focused — one feature or fix per PR
- Test with `python3 generator.py --codes 10100 -v` before submitting
- No dependencies beyond the standard library for `generator.py`
