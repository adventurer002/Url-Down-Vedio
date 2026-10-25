struct Bloom { texel: vec4f }
@group(0) @binding(0) var source: texture_2d<f32>;
@group(0) @binding(1) var linearSampler: sampler;
@group(0) @binding(2) var<uniform> bloom: Bloom;

fn bright(uv: vec2f) -> vec3f {
  let color = textureSampleLevel(source, linearSampler, uv, 0.0).rgb;
  let peak = max(max(color.r, color.g), color.b);
  return color * smoothstep(bloom.texel.z, bloom.texel.z + bloom.texel.w, peak);
}

@fragment
fn fs_main(@location(0) uv: vec2f) -> @location(0) vec4f {
  let o = bloom.texel.xy;
  let total = bright(uv + vec2f(o.x, o.y)) + bright(uv + vec2f(-o.x, o.y)) + bright(uv + vec2f(o.x, -o.y)) + bright(uv - o);
  return vec4f(total * 0.25, 0.0);
}
