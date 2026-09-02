# A tiny web-DOOM image: nginx serving js-dos + the freely-distributable shareware WAD.
# Build on any Docker host, then pack the whole thing INTO a spreadsheet (SICF):
#   docker build -t doom:shareware lab/doom
#   docker save  doom:shareware -o doom.tar
#   python3 /path/to/sci/tools/sheetbuild.py import doom.tar --name doom:shareware --store cluster.xlsx
FROM nginx:alpine
RUN apk add --no-cache curl \
 && curl -fsSL -o /usr/share/nginx/html/doom1.wad \
      https://distro.ibiblio.org/pub/linux/distributions/slitaz/sources/packages/d/doom1.wad \
 || echo "supply any shareware DOOM1.WAD at /usr/share/nginx/html/doom1.wad"
COPY index.html /usr/share/nginx/html/index.html
EXPOSE 80
