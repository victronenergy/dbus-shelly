FILES =					\
	dbus_shelly.py		\
	meter.py

LIB =					\
	__init__.py			\
	service.py			\
	client.py			\
	localsettings.py	\

all:

install:
	install -d $(DESTDIR)$(bindir)
	install -d $(DESTDIR)$(bindir)/ext/aiovelib/aiovelib
	install -m 0644 $(FILES) $(DESTDIR)$(bindir)
	install -m 0644 $(addprefix ext/aiovelib/aiovelib/,$(LIB)) \
		$(DESTDIR)$(bindir)/ext/aiovelib/aiovelib
	chmod +x $(DESTDIR)$(bindir)/$(firstword $(FILES))

testinstall:
	$(eval TMP := $(shell mktemp -d))
	$(MAKE) DESTDIR=$(TMP) install
	(cd $(TMP) && ./dbus_shelly.py --help > /dev/null)
	-rm -rf $(TMP)

clean:
distclean:
