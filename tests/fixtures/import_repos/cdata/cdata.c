/* Fixture: like figlet, it reads a data file from the directory compiled in at build time. */
#include <stdio.h>
int main(void) {
  char line[256]; FILE *f = fopen(DATADIR "/greeting.txt", "r");
  if (!f) { fprintf(stderr, "cdata: %s/greeting.txt: Unable to open data file\n", DATADIR); return 1; }
  if (fgets(line, sizeof line, f)) fputs(line, stdout);
  while (fgets(line, sizeof line, stdin)) fputs(line, stdout);
  return 0;
}
