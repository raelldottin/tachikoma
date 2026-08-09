# Handoff Report — Explorer M1.2: SDK Client Runtime Response-Shape Guards (R3, R4, R5)

## 1. Observation

### Observation 1.1: Room Design Shape Assumptions and Incorrect Exception Message in `sdk/client.py`
- **File & Line Numbers**: `sdk/client.py`, lines 1769-1834 (`upgradeRooms`), lines 1835-1851 (`listUpgradingRooms`), lines 608-622 (`getRoomName`), lines 1664-1682 (`speedUpRoomConstructionUsingBoostGauge`).
- **Verbatim Code snippet (`sdk/client.py`:1769-1793)**:
  ```python
  def upgradeRooms(self):
      try:
          if not hasattr(self, "roomDesigns"):
              self.listRoomDesigns2()
              if "RoomDesign" not in self.roomDesigns:
                  logging.error("ListRoomDesigns endpoint failed.")

          roomDesigns = self.roomDesigns
          self.listUpgradingRooms()
          self.getShipByUserId()
          shipByUserId = self.shipByUserId
          if shipByUserId:
              for room in shipByUserId["ShipService"]["GetShipByUserId"]["Ship"][
                  "Rooms"
              ]["Room"]:
                  ...
                  for roomDesignData in roomDesigns["RoomDesign"]:
  ```
- **Verbatim Code snippet (`sdk/client.py`:1831-1833)**:
  ```python
      except:
          logging.exception("Unable to upgrade research.", exc_info=True)
          return False
  ```
- **Verbatim Code snippet (`sdk/client.py`:1846)**:
  ```python
      for roomDesignData in roomDesigns["RoomDesign"]:
  ```
- **Defects & Unhandled Edge Cases**:
  1. Direct indexing `roomDesigns["RoomDesign"]` assumes `self.roomDesigns` is a dictionary containing key `"RoomDesign"`, and that its value is an iterable `list`.
  2. If `self.roomDesigns` lacks `"RoomDesign"` (e.g. endpoint error response `{"errorMessage": "..."}` or `None`), `if "RoomDesign" not in self.roomDesigns:` logs error but execution continues to `roomDesigns["RoomDesign"]`, raising unhandled `KeyError` (caught by `except:`).
  3. If `"RoomDesign"` is a single dictionary (not a list), `for roomDesignData in roomDesigns["RoomDesign"]:` iterates over dict keys (strings like `'@RoomDesignId'`), raising `TypeError: string indices must be integers`.
  4. Line 1832 logs `"Unable to upgrade research."` inside `upgradeRooms()`. R3 explicitly mandates logging `"Unable to upgrade rooms."`.
  5. Missing required logging on missing/failed room design data: R3 requires logging exactly: `Room design data unavailable; skipping room upgrades.` and returning `False` for endpoint/schema failures.

### Observation 1.2: Research Outcome Handling in `upgradeResearches()` and `addResearch()`
- **File & Line Numbers**: `sdk/client.py`, lines 1708-1768 (`upgradeResearches`), lines 1862-1868 (`addResearch`), lines 145-148 (`request`).
- **Verbatim Code snippet (`sdk/client.py`:1862-1868)**:
  ```python
  def addResearch(self, researchDesignId):
      url = f"https://api.pixelstarships.com/ResearchService/AddResearch?researchDesignId={researchDesignId}&researchStartDate={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}&accessToken={self.accessToken}"
      r = self.request(url, "POST")
      if "errorMessage" in r.text:
          return False
      else:
          return True
  ```
- **Verbatim Code snippet (`sdk/client.py`:145-147)**:
  ```python
  if "errorMessage" in r.text:
      d = xmltodict.parse(r.content, xml_attribs=True)
      logging.error("[%s] {%s} - {%s}", self.info["@Name"], redact_secrets(url), redact_secrets(str(d)))
  ```
- **Verbatim Code snippet (`sdk/client.py`:1754-1763)**:
  ```python
  if not researchingFlag:
      for researchItem in upgradeList:
          if int(researchItem[1]) > 0 and int(researchItem[1]) < int(
              self.gasTotal
          ):
              if self.addResearch(researchItem[0]):
                  logging.info(
                      f"[{self.info['@Name']}] Beginning research for {researchItem[3]}"
                  )
                  researchingFlag = True
                  break
  ```
- **Defects & Unhandled Edge Cases**:
  1. `self.request()` automatically logs any response with `"errorMessage"` as `logging.error(...)`.
  2. R4 specifies that `"Please upgrade your lab room."` is an expected game-state rejection, NOT an application error.
  3. When `AddResearch` returns `"Please upgrade your lab room."`, code must log: `Skipped research design <design_id>: lab upgrade required.`, must NOT log an error, must NOT raise a traceback, and must allow `upgradeResearches()` to continue checking the next research design candidate.
  4. Unexpected errors (other `errorMessage` values, transport failures, malformed XML) must return `False` and contribute to runtime failure status.
  5. Deep indexing `self.allResearchDesigns["ResearchService"]["ListAllResearchDesigns"]["ResearchDesigns"]["ResearchDesign"]` and `self.allResearches["ResearchService"]["ListAllResearches"]["Researches"]["Research"]` assumes list structures and raises `KeyError`/`TypeError` if dict or missing.

### Observation 1.3: Training Data Shape Assumptions in `manageTraining()`
- **File & Line Numbers**: `sdk/client.py`, lines 817-843, lines 1165-1168 (`manageTraining`).
- **Verbatim Code snippet (`sdk/client.py`:838-842, 1165-1167)**:
  ```python
  if not hasattr(self, "trainingDesigns"):
      self.listAllTrainingDesigns2()
      if "TrainingDesign" not in self.trainingDesigns:
          logging.error("TrainingDesign data not available.")
          return False
  ...
  trainingDesignId = None
  for design in self.trainingDesigns["TrainingDesign"]:
      if design["@TrainingName"] == trainingName:
          trainingDesignId = design["@TrainingDesignId"]
  ```
- **Defects & Unhandled Edge Cases**:
  1. `self.trainingDesigns["TrainingDesign"]` assumes top-level dict key `"TrainingDesign"` containing a `list`.
  2. If `TrainingDesign` is a single dictionary, looping iterates over dict keys, causing `TypeError: string indices must be integers`.
  3. Valid no-data condition (valid response with empty/missing training designs) currently logs `TrainingDesign data not available.` as an error and returns `False`. R5 requires treating valid no-data as a successful no-op (`True`), logging a clear skip message.
  4. Endpoint or schema failure must log a sanitized application error and return `False`.

---

## 2. Logic Chain

1. **Observations 1.1, 1.2, 1.3** demonstrate that `sdk/client.py` makes un-guarded shape assumptions across `upgradeRooms()`, `listUpgradingRooms()`, `upgradeResearches()`, `addResearch()`, and `manageTraining()`. Specifically:
   - XML parsing via `xmltodict` yields a single `dict` when a collection has 1 child element, but a `list` when it has 2+ elements, and omits the key when there are 0 elements or when an endpoint returns an error payload.
   - Direct indexing `collection[item_key]` throws `KeyError` when missing/error, or iterates over string keys of a `dict` when only 1 item exists (raising `TypeError`).

2. **Collection Normalization Helper (`_extract_collection` / `_normalize_collection`)**:
   - Creating a private helper function `_extract_collection(data: Any, item_key: str) -> list[dict]` in `sdk/client.py` handles:
     - `data` is `None` or not a `dict`: returns `[]`.
     - `item_key` contains a `list`: filters and returns list of dicts.
     - `item_key` contains a single `dict`: returns `[dict]`.
     - Nested XML wrappers (e.g. `data["RoomService"]["ListRoomDesigns"]["RoomDesigns"]["RoomDesign"]` or top-level `data["RoomDesign"]`): extracts `item_key` wherever present.
   - This solves shape normalization for `RoomDesign`, `ResearchDesign`, `Research`, `TrainingDesign`, and related collections cleanly without broad architectural rewrites.

3. **Logic for R3 (`upgradeRooms()` and `listUpgradingRooms()`)**:
   - In `upgradeRooms()`:
     - Fetch or inspect `self.roomDesigns`.
     - Extract normalized room designs via `_extract_collection(self.roomDesigns, "RoomDesign")`.
     - If `self.roomDesigns` indicates an endpoint error or missing/invalid schema:
       - Log: `Room design data unavailable; skipping room upgrades.`
       - Return `False`.
     - If `room_designs` is empty (valid no-data): log skip message and return `True`.
     - Replace `for roomDesignData in roomDesigns["RoomDesign"]:` with `for roomDesignData in room_designs:`.
     - Fix exception log message in `except:` block: change `"Unable to upgrade research."` to `"Unable to upgrade rooms."`.
   - In `listUpgradingRooms()`:
     - Safely get room designs using `_extract_collection(getattr(self, "roomDesigns", None), "RoomDesign")`.
     - Loop over normalized room design list only if non-empty; avoid direct `roomDesigns["RoomDesign"]` indexing.

4. **Logic for R4 (`upgradeResearches()` and `addResearch()`)**:
   - In `self.request()` (or inside `addResearch()`):
     - Avoid logging `logging.error(...)` when `r.text` contains `"Please upgrade your lab room."`.
   - In `addResearch(self, researchDesignId)`:
     - Inspect response `r`.
     - If `r.text` contains `"Please upgrade your lab room."`:
       - Log: `Skipped research design <design_id>: lab upgrade required.`
       - Return `"LAB_UPGRADE_REQUIRED"` (or a custom enum/tuple `(False, "LAB_UPGRADE_REQUIRED")`).
     - If `r.text` contains other `errorMessage` or is malformed:
       - Log sanitized error, return `False`.
     - If success: return `True`.
   - In `upgradeResearches()`:
     - Safely extract normalized research designs and user researches.
     - Loop through `upgradeList`:
       - Call `status = self.addResearch(design_id)`.
       - If `status is True`: log beginning research, set `researchingFlag = True`, break.
       - If `status == "LAB_UPGRADE_REQUIRED"`: continue to next candidate design in `upgradeList`.
       - If `status is False`: record unexpected failure, return `False`.
     - If process finishes with no unexpected failures: return `True`.

5. **Logic for R5 (`manageTraining()`)**:
   - In `manageTraining()`:
     - Fetch or inspect `self.trainingDesigns`.
     - If `self.trainingDesigns` reflects an endpoint failure or invalid schema:
       - Log sanitized application error.
       - Return `False`.
     - Extract normalized training designs using `_extract_collection(self.trainingDesigns, "TrainingDesign")`.
     - If normalized list is empty (valid no-data condition):
       - Log: `Training design data unavailable; skipping training.` (or `No training designs available; skipping training.`)
       - Return `True` (successful no-op).
     - Replace `for design in self.trainingDesigns["TrainingDesign"]:` with `for design in training_designs:`.

---

## 3. Caveats

- **Scope boundary**: This plan covers changes required in `sdk/client.py`. Integration of process exit codes and SMTP handling in `run.py` (R6, R7) are handled by companion slice explorer/builder roles (`explorer_m1_3`).
- **Network mocking**: All tests covering these scenarios must use synthetic XML/dict fixtures and mock network calls per project rules.

---

## 4. Conclusion

`sdk/client.py` requires precise, non-intrusive shape guards and outcome classification fixes across `upgradeRooms()`, `listUpgradingRooms()`, `upgradeResearches()`, `addResearch()`, and `manageTraining()`. Adding a private static helper `_extract_collection` transparently resolves dictionary vs list vs missing schema shape variations.

---

## 5. Proposed Diff Plan for `sdk/client.py`

### 5.1 Helper Function Addition
Add `_extract_collection` private helper in `sdk/client.py`:
```python
def _extract_collection(data: Any, item_key: str) -> list[dict]:
    """Extract and normalize an item collection from raw parsed dict into a list of dicts.
    
    Supports:
    - Top-level key matching (data[item_key])
    - Nested dict matching (e.g. data["RoomService"]["ListRoomDesigns"]["RoomDesigns"][item_key])
    - Dict to 1-element list conversion
    - Missing/None/invalid data returning []
    """
    if not isinstance(data, dict):
        return []
    if item_key in data:
        val = data[item_key]
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
        if isinstance(val, dict):
            return [val]
        return []
    for val in data.values():
        if isinstance(val, dict):
            res = _extract_collection(val, item_key)
            if res:
                return res
    return []
```

### 5.2 `request()` Update for Expected Rejections
Modify `Client.request()` (around line 145):
```python
if "errorMessage" in r.text:
    if "Please upgrade your lab room." not in r.text:
        d = xmltodict.parse(r.content, xml_attribs=True)
        logging.error("[%s] {%s} - {%s}", self.info["@Name"], redact_secrets(url), redact_secrets(str(d)))
```

### 5.3 `upgradeRooms()` & `listUpgradingRooms()` Refactoring
- In `upgradeRooms()`:
  ```python
  def upgradeRooms(self):
      try:
          if not hasattr(self, "roomDesigns"):
              self.listRoomDesigns2()

          room_designs = _extract_collection(getattr(self, "roomDesigns", None), "RoomDesign")
          if not room_designs:
              # Check if it was an endpoint error vs empty
              logging.info("Room design data unavailable; skipping room upgrades.")
              if not hasattr(self, "roomDesigns") or not isinstance(self.roomDesigns, dict) or "errorMessage" in str(self.roomDesigns):
                  return False
              return True

          self.listUpgradingRooms()
          self.getShipByUserId()
          shipByUserId = getattr(self, "shipByUserId", None)
          if shipByUserId and "ShipService" in shipByUserId:
              rooms = _extract_collection(shipByUserId["ShipService"]["GetShipByUserId"]["Ship"]["Rooms"], "Room")
              for room in rooms:
                  roomId = room.get("@RoomId")
                  roomStatus = room.get("@RoomStatus")
                  roomDesignId = room.get("@RoomDesignId")
                  ...
                  for roomDesignData in room_designs:
                      ...
          return True
      except Exception:
          logging.exception("Unable to upgrade rooms.", exc_info=True)
          return False
  ```

- In `listUpgradingRooms()`:
  ```python
  def listUpgradingRooms(self):
      self.getShipByUserId()
      shipByUserId = getattr(self, "shipByUserId", None)
      room_designs = _extract_collection(getattr(self, "roomDesigns", None), "RoomDesign")
      if shipByUserId and room_designs and "ShipService" in shipByUserId:
          rooms = _extract_collection(shipByUserId["ShipService"]["GetShipByUserId"]["Ship"]["Rooms"], "Room")
          for room in rooms:
              if room.get("@RoomStatus") == "Upgrading":
                  for roomDesignData in room_designs:
                      if room.get("@RoomDesignId") == roomDesignData.get("@RoomDesignId"):
                          logging.info(
                              f"[{self.info['@Name']}] {''.join(roomDesignData.get('@RoomName', ''))} is currently being upgraded."
                          )
  ```

### 5.4 `addResearch()` & `upgradeResearches()` Refactoring
- In `addResearch()`:
  ```python
  def addResearch(self, researchDesignId):
      url = f"https://api.pixelstarships.com/ResearchService/AddResearch?researchDesignId={researchDesignId}&researchStartDate={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}&accessToken={self.accessToken}"
      r = self.request(url, "POST")
      if r and "Please upgrade your lab room." in r.text:
          logging.info(f"Skipped research design {researchDesignId}: lab upgrade required.")
          return "LAB_UPGRADE_REQUIRED"
      if r and "errorMessage" in r.text:
          return False
      return True
  ```

- In `upgradeResearches()`:
  ```python
  def upgradeResearches(self):
      try:
          self.listAllResearches()
          self.listAllResearchDesigns2()

          all_researches = _extract_collection(getattr(self, "allResearches", None), "Research")
          all_research_designs = _extract_collection(getattr(self, "allResearchDesigns", None), "ResearchDesign")

          upgradeList = []
          rootDesigns = collections.defaultdict(list)
          designExceptionList = []
          rootDesignExceptionList = []
          researchingFlag = False

          for research in all_researches:
              for design in all_research_designs:
                  if (
                      research.get("@ResearchDesignId") == design.get("@ResearchDesignId")
                      and design.get("@ResearchDesignId") not in designExceptionList
                  ):
                      if research.get("@ResearchState") == "Researching":
                          logging.info(
                              f"[{self.info['@Name']}] {''.join(design.get('@ResearchName', ''))} is currently being researched."
                          )
                          researchingFlag = True
                      designExceptionList.append(design.get("@ResearchDesignId"))

          for design in all_research_designs:
              if (
                  design.get("@ResearchDesignId") not in designExceptionList
                  and design.get("@RootResearchDesignId") not in rootDesignExceptionList
              ):
                  rootDesigns[design.get("@RootResearchDesignId")].append(design)
                  upgradeList.append(
                      [
                          design.get("@ResearchDesignId"),
                          design.get("@GasCost", "0"),
                          design.get("@StarbuxCost", "0"),
                          design.get("@ResearchName", ""),
                      ]
                  )
                  rootDesignExceptionList.append(design.get("@RootResearchDesignId"))

          self.collectAllResources()
          if not researchingFlag:
              for researchItem in upgradeList:
                  if int(researchItem[1]) > 0 and int(researchItem[1]) < int(self.gasTotal):
                      res = self.addResearch(researchItem[0])
                      if res is True:
                          logging.info(
                              f"[{self.info['@Name']}] Beginning research for {researchItem[3]}"
                          )
                          researchingFlag = True
                          break
                      elif res == "LAB_UPGRADE_REQUIRED":
                          continue
                      else:
                          return False
          return True
      except Exception:
          logging.exception("Unable to upgrade research.", exc_info=True)
          return False
  ```

### 5.5 `manageTraining()` Refactoring
- In `manageTraining()`:
  ```python
  def manageTraining(self):
      ...
      if not hasattr(self, "trainingDesigns"):
          self.listAllTrainingDesigns2()

      training_designs = _extract_collection(getattr(self, "trainingDesigns", None), "TrainingDesign")
      if not training_designs:
          if not hasattr(self, "trainingDesigns") or not isinstance(self.trainingDesigns, dict) or "errorMessage" in str(self.trainingDesigns):
              logging.error("TrainingService endpoint failed or returned invalid schema.")
              return False
          logging.info("Training design data unavailable; skipping training.")
          return True
      ...
      trainingDesignId = None
      for design in training_designs:
          if design.get("@TrainingName") == trainingName:
              trainingDesignId = design.get("@TrainingDesignId")
      ...
  ```

---

## 6. Verification Method

1. **Syntax Check**: `make syntax-check`
2. **Linting**: `make lint`
3. **Unit Tests**: `make test` (and new test suite in `tests/test_runtime_shape_guards.py`)
4. **Security Tests**: `make test-security`
