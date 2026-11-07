# Minimal Sphinx Makefile: `make -C docs html`, `make -C docs strict`, `make -C docs clean`.
SPHINXBUILD ?= uv run --group docs sphinx-build
SOURCEDIR    = .
BUILDDIR     = _build

.PHONY: html strict clean

html:
	$(SPHINXBUILD) -b html "$(SOURCEDIR)" "$(BUILDDIR)/html"

strict:
	$(SPHINXBUILD) -W --keep-going -b html "$(SOURCEDIR)" "$(BUILDDIR)/html"

clean:
	rm -rf "$(BUILDDIR)"
