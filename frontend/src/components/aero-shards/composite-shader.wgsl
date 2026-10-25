struct Blend { info: vec4f }
@group(0) @binding(0) var source: texture_2d<f32>;
@group(0) @binding(1) var linearSampler: sampler;
@group(0) @binding(2) var<uniform> mixInfo: Blend;

@fragment
fn fs_main(@location(0) uv: vec2f) -> @location(0) vec4f {
  return textureSampleLevel(source, linearSampler, uv, 0.0) * mixInfo.info.x;
}
