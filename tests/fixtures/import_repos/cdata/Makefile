prefix = /usr/local
DATADIR = $(prefix)/share/cdata
CFLAGS += -DDATADIR=\"$(DATADIR)\"

cdata: cdata.c
	$(CC) $(CFLAGS) -o cdata cdata.c

install: cdata
	mkdir -p $(prefix)/bin $(DATADIR)
	cp cdata $(prefix)/bin/cdata
	cp data/greeting.txt $(DATADIR)/greeting.txt
