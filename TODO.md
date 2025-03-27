# TODO

If you encounter problems during the integration process, you can create an issue in the ISSUE section of the repository. Similarly, if you have other requirements, you can create a topic for discussion in the DISCUSSION section of the repository. If everyone deems the requirements reasonable, we will provide support in the future.

## Features
- [ ] Support cross-device entity connection. For example, the air conditioner companion supports binding other entities with temperature and humidity sensors, or other connection rules.
- [ ] Supports converting Xiaoai audio playback to a media player entity and can attempt to parse and display the played audio information.
- [ ] Support entity filtering, and you can select the entities to be displayed.
- [ ] Support multi-language translation configuration.
- [ ] Supports invoking the manual control scenarios of Xiaomi Home.

## Bugs

## Improvements
- [ ] Reconstruct the climate entity.
- [ ] When integrating overloading, events need to be avoided from being triggered. You can try to adjust the entity state refresh logic to solve this problem.
- [ ] Optimize the logic of the equipment control link, such as information like exception capture and error prompts.
- [ ] Optimize the device event logic.
- [ ] Optimize the device online and offline logic, such as creating a timer to refresh the device status regularly.
- [ ] The human presence sensor can be attempted to be converted into a binary_sensor entity.
- [ ] The status of the department's doors and windows needs to be reversed, and it should be modified according to the actual online devices.
- [ ] The integration can try to block the subscription messages of props and events, and only process spec messages to avoid printing error logs.

## Documentation
- [ ] Improve the usage documentation, such as the introduction of various configuration parameters.

## Testing
- [ ] Improve the LAN service test cases. Simulated device testing can be used.

## Miscellaneous
