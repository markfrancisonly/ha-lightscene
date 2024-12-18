<!-- markdownlint-disable first-line-heading -->
<!-- markdownlint-disable no-inline-html -->

[![GitHub Release](https://img.shields.io/github/release/markfrancisonly/ha-lightscene.svg?style=flat-square)](https://github.com/markfrancisonly/ha-lightscene/releases)
[![License](https://img.shields.io/github/license/markfrancisonly/ha-lightscene.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-default-orange.svg?style=flat-square)](https://hacs.xyz)


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

1. Copy the `lightscene` component folder into your Home Assistant `custom_components` directory.

2. Restart Home Assistant to enable the component.
 

## Development Notes

The component includes the following key classes:

-  **LightSceneManager**: Manages discovery and lifecycle of `LightScene` entities.
-  **LightScene**: Represents a single light scene with brightness scaling and context management.

  
## Contributing

Contributions are welcome! Submit an issue or create a pull request on GitHub to propose improvements or report bugs.


## License

This component is licensed under the MIT License. See the `LICENSE` file for more details.

---

Happy automating! 🎉
