# Contingency Atlas whitepaper — Top-level build system
# ======================================================
# Usage:
#   make all       — regenerate all figures + compile PDF
#   make figures   — regenerate figures only (reads generated/redesign/ artifacts)
#   make pdf       — compile PDF only (assumes figures exist)
#   make clean     — remove LaTeX build artifacts
#   make open      — open the compiled PDF

SCRIPTS  = scripts
FIGURES  = figures
MANU     = manuscript
TEX      = $(MANU)/contingency_atlas_whitepaper.tex
PDF      = $(MANU)/contingency_atlas_whitepaper.pdf

# Override LATEXMK when the executable is not on PATH.
LATEXMK ?= latexmk
PYTHON  ?= ../../.venv/bin/python

# ------------------------------------------------
all: figures pdf

figures:
	@echo "=== Generating figures 1-4 from evaluation artifacts ==="
	cd $(SCRIPTS) && $(PYTHON) generate_figures.py
	@echo "=== All figures generated ==="

pdf:
	@echo "=== Compiling PDF ==="
	cd $(MANU) && $(LATEXMK) -pdf -interaction=nonstopmode -f contingency_atlas_whitepaper.tex
	@echo "=== PDF compiled: $(PDF) ==="

clean:
	cd $(MANU) && $(LATEXMK) -C
	rm -f $(MANU)/tmp*.tmp

open: $(PDF)
	open $(PDF)

.PHONY: all figures pdf clean open
