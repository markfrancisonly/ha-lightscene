<!-- markdownlint-disable first-line-heading -->
<!-- markdownlint-disable no-inline-html -->

[![GitHub Release](https://img.shields.io/github/release/markfrancisonly/ha-lightscene.svg?style=flat-square)](https://github.com/markfrancisonly/ha-lightscene/releases)
[![License](https://img.shields.io/github/license/markfrancisonly/ha-lightscene.svg?style=flat-square)](https://github.com/markfrancisonly/ha-lightscene/blob/master/LICENSE)


# LightScene Integration for Home Assistant

**LightScene** is a custom Home Assistant integration that enhances your lighting control by managing scenes with dynamic brightness scaling and context tracking. Enable brightness scaling and context tracking for  Home Assistant `scene` entities, allowing dynamic control over lighting environments in your smart home. 

This component introduces `scene`off functionality and proportional brightness control for lights defined in your Home Assistant `scene`.
    
## Features

-  **Automatic discovery**: Automatically detects scenes and creates a corresponding `light` entity for every Home Assistant scene. 
- **Scene state tracking**: Listens for `scene` activation events to turn on *LightScene* entities. Tracks scene entity changes to determine if scene has been turned off.
- **Togglable**: Regular scenes only support being turned on. *LightScene* lights can be turned on or off. 
-  **Proportional brightness control**: Scale brightness of all `scene` lights in proportion to `scene` presets.

## Configuration

No manual configuration is needed. The component automatically discovers `scene` entities and creates corresponding *LightScene* entities.

### Example

For example, a scene named `Evening Lights` with several lights with brightness controls will automatically have a *LightScene* `light` entity created with brightness scaling enabled.

## Usage

### Turning On a Light Scene

Light scenes can be turned on via the Home Assistant UI or through a service call, as usual:

```yaml
service: light.turn_on
data:
  entity_id: light.evening_lights
  brightness: 150
```

### Turning Off a Light Scene

Turn off the light scene with:

```yaml
service: light.turn_off
data:
  entity_id: light.evening_lights
```

### Dynamic Brightness Scaling

The component scales brightness based on the baseline brightness of the scene. For example:

- A light with brightness 255 in the scene will scale proportionally to match the specified brightness.

## Events

-  **Scene reload**: Automatically updates all `LightScene` entities when scenes are reloaded.
-  **Scene activation**: Handles external activations of scenes and updates the corresponding `LightScene` entity.

## Logs and Debugging

To enable debug logging for this component, add the following to your `configuration.yaml`:

```yaml

logger:
  default: warning
  logs:
    custom_components.lightscene: debug
```

## Installation

### HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=markfrancisonly&repository=ha-lightscene&category=integration)

Or manually add the custom repository:

<details>
<summary>Step-by-step HACS installation</summary>

1. Open **HACS** in your Home Assistant dashboard
2. Click the **⋮** menu (top right) → **Custom repositories**
3. Add this URL and set the category to **Integration**, then click **Add**:
   ```
   https://github.com/markfrancisonly/ha-lightscene
   ```
4. The repository now appears in the custom repositories list. Close the dialog.
5. Back in HACS, search for **Video Call** and open the result
6. Click **Download** (or **Install**) and confirm
7. **Restart Home Assistant**

</details>

### Manual Installation

1. Download the [latest release](https://github.com/markfrancisonly/ha-lightscene/releases)
2. Copy the contents into `custom_components/ha-lightscene/` inside your HA config directory
3. Restart Home Assistant
