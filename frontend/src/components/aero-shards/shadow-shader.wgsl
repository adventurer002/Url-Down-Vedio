struct Shade {
  info: vec4f,
  more: vec4f,
}
@group(0) @binding(0) var source: texture_2d<f32>;
@group(0) @binding(1) var depthMap: texture_depth_2d;
@group(0) @binding(2) var<uniform> shadeInfo: Shade;

@fragment
fn fs_main(@builtin(position) pixel: vec4f) -> @location(0) vec4f {
  let bounds = vec2i(textureDimensions(source)) - vec2i(1);
  let here = vec2i(pixel.xy);
  let color = textureLoad(source, here, 0);
  if (color.a <= 0.002 || shadeInfo.info.z <= 0.0) { return color; }
  let depth = textureLoad(depthMap, here, 0);
  let east = textureLoad(depthMap, clamp(here + vec2i(1, 0), vec2i(0), bounds), 0);
  let west = textureLoad(depthMap, clamp(here - vec2i(1, 0), vec2i(0), bounds), 0);
  let south = textureLoad(depthMap, clamp(here + vec2i(0, 1), vec2i(0), bounds), 0);
  let north = textureLoad(depthMap, clamp(here - vec2i(0, 1), vec2i(0), bounds), 0);
  let limit = shadeInfo.more.x * 0.5;
  let slopeX = clamp(select(east - depth, depth - west, abs(depth - west) < abs(east - depth)), -limit, limit);
  let slopeY = clamp(select(south - depth, depth - north, abs(depth - north) < abs(south - depth)), -limit, limit);
  var occlusion = 0.0;
  for (var i = 1; i <= 12; i += 1) {
    let offset = -shadeInfo.info.xy * f32(i);
    let probe = clamp(vec2i(pixel.xy + offset), vec2i(0), bounds);
    let other = textureLoad(depthMap, probe, 0);
    let expected = depth + slopeX * offset.x + slopeY * offset.y;
    let closer = smoothstep(shadeInfo.more.x * 0.6, shadeInfo.more.x * 2.4, expected - other);
    let reach = 1.0 - f32(i - 1) / 12.0;
    occlusion = max(occlusion, closer * reach * reach);
  }
  return vec4f(color.rgb * (1.0 - occlusion * shadeInfo.info.z), color.a);
}
