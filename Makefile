# Sheeternetes on-prem — bare-metal cluster over a desktop spreadsheet.
# Quickstart:  cp .skctl.env.example .skctl.env  &&  make up  (then, elsewhere) make node
WORKBOOK ?= cluster.xlsx
PORT     ?= 8787

.PHONY: help up node apply pods nodes events tour test down clean
help:            ## show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t—/' | sort

up:              ## run the apiserver (control plane) over $(WORKBOOK)
	WORKBOOK=$(WORKBOOK) PORT=$(PORT) python3 apiserver.py

node:            ## run a kubelet on this host (needs docker; reads .skctl.env)
	./kubelet.sh

apply:           ## apply the demo manifest
	./skctl apply lab/hello-web.json

pods nodes events: ## show pods / nodes / events
	./skctl get $@

tour:            ## guided demo
	./skctl tour

test:            ## run the unit + integration suite
	python3 -m pytest -q

down:            ## stop demo workloads (scale everything created by the lab to 0)
	-./skctl scale web 0
	-./skctl scale hello 0

clean:           ## remove the local workbook (destroys cluster state)
	@echo "rm -f $(WORKBOOK)  # run this yourself to wipe cluster state"
