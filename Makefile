FILES =					\
	dbus_shelly.py		\
	meter.py			\
	discovery.py		\
	shelly_device.py	\
	shelly_handlers.py	\
	shelly_s2.py		\
	shelly_s2_control.py	\
	utils.py

LIB =					\
	__init__.py			\
	service.py			\
	client.py			\
	localsettings.py	\
	s2.py				\

MOCK_FILES = $(wildcard mock/*)

all:

install:
	install -d $(DESTDIR)$(bindir)
	install -d $(DESTDIR)$(bindir)/ext/aiovelib/aiovelib
	install -d $(DESTDIR)$(bindir)/mock
	install -m 0644 $(FILES) $(DESTDIR)$(bindir)
	install -m 0644 $(addprefix ext/aiovelib/aiovelib/,$(LIB)) \
		$(DESTDIR)$(bindir)/ext/aiovelib/aiovelib
	install -m 0644 $(MOCK_FILES) $(DESTDIR)$(bindir)/mock
	chmod +x $(DESTDIR)$(bindir)/$(firstword $(FILES))

testinstall:
	$(eval TMP := $(shell mktemp -d))
	$(MAKE) DESTDIR=$(TMP) install
	(cd $(TMP) && python3 dbus_shelly.py --help > /dev/null)
	-rm -rf $(TMP)

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# make test                          runs all tests
# make test TEST=test_flood          runs matching tests (pytest -k)
# make test TEST="em1 and not phase"
test:
	$(PYTHON) -m pytest -v tests $(if $(TEST),-k "$(TEST)")

test-list:
	@$(PYTHON) -m pytest --collect-only -q tests | grep '::'

clean:
distclean:
