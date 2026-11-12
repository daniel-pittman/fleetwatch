# Surname list

`census_surnames_2010.txt.gz` holds the 162,253 surnames that occur 100 or more times in the
2010 US Census, lowercased, one per line, sorted, gzipped. The privacy scanner reads it to
decide whether an all-capital word beside a name ("Jean DUPONT") is a surname rather than an
acronym.

Source: US Census Bureau, "Frequently Occurring Surnames from the 2010 Census",
<https://www.census.gov/topics/population/genealogy/data/2010_surnames.html>, file
<https://www2.census.gov/topics/genealogy/2010surnames/names.zip> (`Names_2010Census.csv`,
column `name`, the summary row "ALL OTHER NAMES" left out).

Status: a work of the US federal government, in the public domain in the United States
(17 U.S.C. 105).

To rebuild it: download the zip, then

```python
import csv, gzip, io
rows = csv.DictReader(open("Names_2010Census.csv"))
names = sorted({r["name"].lower() for r in rows if r["rank"] != "0"})
buf = io.BytesIO()
with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as g:
    g.write(("\n".join(names) + "\n").encode())
open("census_surnames_2010.txt.gz", "wb").write(buf.getvalue())
```
