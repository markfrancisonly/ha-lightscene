<!-- markdownlint-disable first-line-heading -->
<!-- markdownlint-disable no-inline-html -->

[![GitHub Release](https://img.shields.io/github/release/markfrancisonly/hass-lightscene.svg?style=flat-square)](https://github.com/markfrancisonly/hass-lightscene/releases)
[![Build Status](https://img.shields.io/github/workflow/status/markfrancisonly/hass-lightscene/Build?style=flat-square)](https://github.com/markfrancisonly/hass-lightscene/actions/workflows/build.yaml)
[![License](https://img.shields.io/github/license/markfrancisonly/hass-lightscene.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-default-orange.svg?style=flat-square)](https://hacs.xyz)

# LightScene

LightScene integration for Home Assistant

- scenes can now be operated as a light and be turned on or off
- tracks scene entity changes to determine scene on/off state useful for buttons
- minimal configuration; platform creates a light for every scene defined automatically



## Installation

Easiest install is via [HACS](https://hacs.xyz/):

`HACS -> Explore & Add Repositories -> Todo`


Config flow is not supported yet. After installing the custom component via HACS, the lightscene integration platform must be manually added to the light sections of your configuration.yaml:

```yaml
light:
  - platform: lightscene
```
