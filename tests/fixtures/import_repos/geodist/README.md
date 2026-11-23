# geodist

Calculate the distance between two points on Earth from their latitude and longitude.

## Usage

```python
from geodist import geodist, Unit

lyon = (45.7597, 4.8422) # (lat, lon)
paris = (48.8567, 2.3508)

geodist(lyon, paris)
>> 392.2172595594006  # in kilometers

geodist(lyon, paris, unit=Unit.MILES)
>> 243.7125060953981
```
