"""Perspective framing in UE's X-forward, Y-right, Z-up coordinate system."""
import itertools
import math


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def corners(center, extent):
    return [[center[i] + signs[i] * extent[i] for i in range(3)] for signs in itertools.product((-1, 1), repeat=3)]


def basis(view):
    yaw, elevation = math.radians(view["yaw"]), math.radians(view["elevation"])
    outward = [math.cos(elevation) * math.cos(yaw), math.cos(elevation) * math.sin(yaw), math.sin(elevation)]
    right = [math.sin(yaw), -math.cos(yaw), 0.0]
    up = [-math.sin(elevation) * math.cos(yaw), -math.sin(elevation) * math.sin(yaw), math.cos(elevation)]
    return outward, right, up


def frame(center, extent, profile):
    if not all(math.isfinite(v) for v in center + extent) or max(extent) <= 1e-5:
        raise ValueError("No finite non-empty visible bounds")
    extent = [v + float(profile["padding"]) for v in extent]
    near = min(10.0, max(extent))
    tangent = math.tan(math.radians(profile["fov"] / 2.0)) * (1 - 2 * profile["margin"])
    distance = 0.0
    points = corners([0, 0, 0], extent)
    for view in profile["views"]:
        outward, right, up = basis(view)
        for point in points:
            distance = max(distance, dot(point, outward) + max(abs(dot(point, right)), abs(dot(point, up))) / tangent,
                           dot(point, outward) + near * 1.2)
    result = {"center": center, "distance": distance * 1.001, "fov": profile["fov"]}
    if near < 10:
        result["near_cm"] = near
    return result


def projection(center, extent, framing, view, size):
    outward, right, up = basis(view)
    target = framing["center"]
    tangent = math.tan(math.radians(framing["fov"] / 2))
    pixels = []
    for point in corners(center, extent):
        relative = [point[i] - target[i] for i in range(3)]
        depth = framing["distance"] - dot(relative, outward)
        if depth <= framing.get("near_cm", 10):
            raise ValueError("Bounds intersect near clipping plane")
        pixels.append([(dot(relative, right) / (depth * tangent) + 1) * size / 2,
                       (1 - dot(relative, up) / (depth * tangent)) * size / 2])
    return [min(p[0] for p in pixels), min(p[1] for p in pixels), max(p[0] for p in pixels), max(p[1] for p in pixels)]


def fits(center, extent, framing, profile):
    edge, size = profile["margin"] * profile["size"], profile["size"]
    try:
        return all((lambda box: box[0] >= edge and box[1] >= edge and box[2] <= size - edge and box[3] <= size - edge)(
            projection(center, [v + profile["padding"] for v in extent], framing, view, size)) for view in profile["views"])
    except ValueError:
        return False


def matrices(framing, view):
    """Row-major matrices applied to column vectors; positive camera Z is forward."""
    outward, right, up = basis(view)
    forward = [-v for v in outward]
    location = [framing["center"][i] + framing["distance"] * outward[i] for i in range(3)]
    view_matrix = [axis + [-dot(axis, location)] for axis in (right, up, forward)] + [[0, 0, 0, 1]]
    scale = 1 / math.tan(math.radians(framing["fov"] / 2))
    near = framing.get("near_cm", 10)
    # UE perspective uses an infinite far plane and reversed depth.
    projection_matrix = [[scale, 0, 0, 0], [0, scale, 0, 0], [0, 0, 0, near], [0, 0, 1, 0]]
    return {"convention": "row_major_column_vectors_camera_xyz_right_up_forward_reversed_z",
            "view": view_matrix, "projection": projection_matrix, "near_cm": near}
