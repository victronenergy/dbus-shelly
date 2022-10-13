FILES =					\
	dbus_shelly.py		\
	meter.py			\
	mdns.py

VELIB =					\
	settingsdevice.py	\
	ve_utils.py			\
	vedbus.py			\

all:

install:
	install -d $(DESTDIR)$(bindir)
	install -d $(DESTDIR)$(bindir)/ext/velib_python
	install -m 0644 $(FILES) $(DESTDIR)$(bindir)
	install -m 0644 $(addprefix ext/velib_python/,$(VELIB)) \
		$(DESTDIR)$(bindir)/ext/velib_python
	chmod +x $(DESTDIR)$(bindir)/$(firstword $(FILES))

testinstall:
	$(eval TMP := $(shell mktemp -d))
	$(MAKE) DESTDIR=$(TMP) install
	(cd $(TMP) && ./dbus_shelly.py --help > /dev/null)
	-rm -rf $(TMP)

clean:
