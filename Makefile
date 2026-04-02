FILES =					\
	dbus_shelly.py		\
	meter.py			\
	discovery.py		\
	shelly_device.py	\
	shelly_handlers.py	\
	shelly_s2.py		\
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

clean:
distclean:
