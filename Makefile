SHELL := /bin/bash
SVC ?= hardware-monitor
SUDO ?= sudo

.PHONY: help start stop restart status update

help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "  start    Start the $(SVC) service"
	@echo "  stop     Stop the $(SVC) service"
	@echo "  restart  Restart the $(SVC) service"
	@echo "  status   Show the $(SVC) service status"
	@echo "  update   Pull latest changes from git and restart the service"

start:
	@echo "Starting $(SVC)..."
	$(SUDO) systemctl start $(SVC)
	@echo "Done."

stop:
	@echo "Stopping $(SVC)..."
	$(SUDO) systemctl stop $(SVC)
	@echo "Done."

restart:
	@echo "Restarting $(SVC)..."
	$(SUDO) systemctl restart $(SVC)
	@echo "Done."

status:
	systemctl status $(SVC) --no-pager

update:
	@echo "Pulling latest changes..."
	git pull
	@echo "Restarting $(SVC)..."
	$(SUDO) systemctl restart $(SVC)
	@echo "Done."
