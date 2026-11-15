# AUTO-GENERATED — DO NOT EDIT.
# Written from platform/helium/common/enums.py by:
#   platform/bin/sync-shared-constants.py

from typing import Final, Literal

RESOURCE_STATUSES: Final = (
    "Owned",
    "Rented",
    "Ordered",
    "Shipped",
    "Needed",
    "Received",
    "To Sell",
    "Digital",
)
RESOURCE_CONDITIONS: Final = (
    "Brand New",
    "Refurbished",
    "Used - Like New",
    "Used - Very Good",
    "Used - Good",
    "Used - Acceptable",
    "Used - Poor",
    "Broken",
    "Digital",
)
UNGRADED: Final = "-1/100"
NO_GRADE: Final = -1
RECURRENCE_HORIZON_YEARS: Final = 5
GradeScalePreset = Literal[
    "4.0",
    "4.0 with +/-",
    "4.33 with +/-",
    "4.5",
    "7.0",
    "9.0",
    "10.0",
    "1.0-5.0 (pass at 50%)",
    "1.0-5.0 (pass at 75%)",
]
GRADE_SCALE_PRESETS: Final[dict[GradeScalePreset, int]] = {
    "4.0": 1,
    "4.0 with +/-": 2,
    "4.33 with +/-": 3,
    "4.5": 4,
    "7.0": 5,
    "9.0": 6,
    "10.0": 7,
    "1.0-5.0 (pass at 50%)": 8,
    "1.0-5.0 (pass at 75%)": 9,
}
