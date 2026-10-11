"""Project JSON format (version 1).

Every model allows extra fields so newer editor features never get stripped by an older
backend, and every field has a default so partial projects load cleanly.
Times are in seconds (floats). Timeline position = clip.start; source range = clip.inPoint..outPoint.
"""
from __future__ import annotations

import time
import uuid
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

PROJECT_VERSION = 1


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Crop(Base):
    left: float = 0
    top: float = 0
    right: float = 0
    bottom: float = 0  # fractions 0..1 cut from each edge


class Transform(Base):
    x: float = 0  # offset from canvas centre, in canvas pixels
    y: float = 0
    scale: float = 1
    rotation: float = 0  # degrees
    flipH: bool = False
    flipV: bool = False
    opacity: float = 1
    crop: Crop = Field(default_factory=Crop)


class Keyframe(Base):
    time: float  # seconds relative to the clip start
    value: float
    easing: str = "linear"


class Filter(Base):
    id: str = Field(default_factory=new_id)
    type: str  # "adjust" | "preset" | "effect" | "lut"
    name: str = ""
    intensity: float = 1
    params: dict = Field(default_factory=dict)
    enabled: bool = True


class TextStyle(Base):
    text: str = ""
    font: str = "Inter"
    size: float = 72
    color: str = "#ffffff"
    outlineColor: str = "#000000"
    outlineWidth: float = 0
    shadow: bool = False
    box: bool = False
    boxColor: str = "#00000099"
    align: str = "center"
    animIn: str = "none"
    animOut: str = "none"


class Clip(Base):
    id: str = Field(default_factory=new_id)
    mediaId: Optional[str] = None  # None for text clips
    start: float = 0
    inPoint: float = 0
    outPoint: float = 0
    speed: float = 1
    reverse: bool = False
    freezeFrame: Optional[float] = None
    volume: float = 1
    muted: bool = False
    fadeIn: float = 0
    fadeOut: float = 0
    fit: Optional[Literal["fit", "fill", "blur"]] = None  # None = project default
    transform: Transform = Field(default_factory=Transform)
    keyframes: dict[str, list[Keyframe]] = Field(default_factory=dict)
    filters: list[Filter] = Field(default_factory=list)
    text: Optional[TextStyle] = None
    adjustment: bool = False  # adjustment layer (overlay track, no media): grades everything below it

    @property
    def duration(self) -> float:
        return max(0.0, (self.outPoint - self.inPoint) / (self.speed or 1))


class Transition(Base):
    id: str = Field(default_factory=new_id)
    trackId: str
    fromClipId: str
    toClipId: str
    type: str = "fade"
    duration: float = 0.5


class Track(Base):
    id: str = Field(default_factory=new_id)
    type: Literal["video", "overlay", "text", "audio", "music"] = "video"
    name: str = ""
    muted: bool = False
    hidden: bool = False
    locked: bool = False
    volume: float = 1
    magnetic: Optional[bool] = None  # main video track: clips stay packed (default on)
    clips: list[Clip] = Field(default_factory=list)


class Marker(Base):
    id: str = Field(default_factory=new_id)
    time: float
    label: str = ""
    color: str = "#f5c542"


class Background(Base):
    mode: Literal["fit", "fill", "blur"] = "fit"
    color: str = "#000000"


class Settings(Base):
    width: int = 1080
    height: int = 1920
    fps: float = 30.0
    aspect: str = "9:16"
    background: Background = Field(default_factory=Background)
    sampleRate: int = 48000


class ExportRange(Base):
    start: float = 0
    end: float = 0


class ExportSettings(Base):
    preset: str = "reels"
    mode: Literal["final", "draft"] = "final"
    width: int = 1080
    height: int = 1920
    fps: Union[float, Literal["source"]] = 30.0
    range: Optional[ExportRange] = None
    fileName: str = ""


class Project(Base):
    version: int = PROJECT_VERSION
    id: str = Field(default_factory=new_id)
    name: str = "Untitled project"
    created: float = Field(default_factory=time.time)
    updated: float = Field(default_factory=time.time)
    settings: Settings = Field(default_factory=Settings)
    media: list[str] = Field(default_factory=list)  # media ids (metadata lives in the media registry)
    tracks: list[Track] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    markers: list[Marker] = Field(default_factory=list)
    export: ExportSettings = Field(default_factory=ExportSettings)


def default_tracks() -> list[Track]:
    return [
        Track(type="video", name="Main video"),
        Track(type="overlay", name="Overlay"),
        Track(type="text", name="Text"),
        Track(type="audio", name="Audio"),
        Track(type="music", name="Music"),
    ]


ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1920, 1080)}


def new_project(name: str, aspect: str = "9:16") -> Project:
    w, h = ASPECTS.get(aspect, ASPECTS["9:16"])
    return Project(
        name=name or "Untitled project",
        settings=Settings(width=w, height=h, aspect=aspect),
        tracks=default_tracks(),
        export=ExportSettings(width=w, height=h,
                              preset={"9:16": "reels", "1:1": "square", "16:9": "youtube"}.get(aspect, "custom")),
    )
