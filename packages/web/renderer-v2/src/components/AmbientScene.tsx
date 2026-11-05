import { useEffect, useRef } from 'react';
import * as THREE from 'three';

/**
 * 各页面专属的三维背景：
 * - model  ：模型服务页 —— 神经网络核心（线框核 + 轨道节点 + 数据尘）
 * - access ：账号权限页 —— 六边形权限盾（同心六边形 + 权限格栅 + 扫描束）
 * - workspace（WorkspaceScene）：项目详情 —— 标书工作面（纸张 + 章节块 + 步骤动效）
 */

const PALETTE = {
  indigo: 0x6366f1,
  violet: 0x8b5cf6,
  cyan: 0x22d3ee,
  teal: 0x0d9488,
  paper: 0xe9e6ff,
};

const STEP_ACCENT: Record<string, number> = {
  overview: PALETTE.indigo,
  files: PALETTE.cyan,
  analysis: PALETTE.cyan,
  writing: PALETTE.violet,
  review: PALETTE.teal,
  delivery: PALETTE.indigo,
};

type Stage = {
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  renderer: THREE.WebGLRenderer;
  compact: boolean;
};

function lineMaterial(color: number, opacity: number) {
  return new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
}

function fillMaterial(color: number, opacity: number) {
  return new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity,
    side: THREE.DoubleSide,
    depthWrite: false,
  });
}

function glowMaterial(color: number, opacity: number) {
  return new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
}

function makeStage(container: HTMLElement, fov = 42): Stage {
  const compact = window.matchMedia('(max-width: 720px)').matches;
  const width = container.clientWidth || 800;
  const height = container.clientHeight || 420;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(fov, width / height, 0.1, 160);
  camera.position.set(0, 0, 12);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, compact ? 1.5 : 2));
  renderer.setSize(width, height);
  renderer.domElement.style.width = '100%';
  renderer.domElement.style.height = '100%';
  container.appendChild(renderer.domElement);
  return { scene, camera, renderer, compact };
}

function disposeStage(stage: Stage) {
  stage.renderer.dispose();
  stage.scene.traverse((object) => {
    if (
      object instanceof THREE.Mesh ||
      object instanceof THREE.Line ||
      object instanceof THREE.LineLoop ||
      object instanceof THREE.LineSegments ||
      object instanceof THREE.Points
    ) {
      object.geometry.dispose();
      const material = object.material as THREE.Material | THREE.Material[];
      if (Array.isArray(material)) material.forEach((item) => item.dispose());
      else material.dispose();
    }
  });
  if (stage.renderer.domElement.parentNode) {
    stage.renderer.domElement.parentNode.removeChild(stage.renderer.domElement);
  }
}

function hexPoints(radius: number, rotation = 0): THREE.Vector3[] {
  const points: THREE.Vector3[] = [];
  for (let index = 0; index < 6; index += 1) {
    const angle = rotation + (index / 6) * Math.PI * 2;
    points.push(new THREE.Vector3(Math.cos(angle) * radius, Math.sin(angle) * radius, 0));
  }
  return points;
}

/* ───────────────────────── 模型服务：神经网络核心 ───────────────────────── */

function buildModelScene(container: HTMLDivElement): () => void {
  const stage = makeStage(container, 40);
    const { scene, camera, renderer, compact } = stage;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    const group = new THREE.Group();
    group.position.set(compact ? 0 : 4.4, 0.2, 0);
    group.scale.setScalar(compact ? 0.9 : 1.05);
    scene.add(group);

    const shell = new THREE.Mesh(
      new THREE.IcosahedronGeometry(2.5, 1),
      new THREE.MeshBasicMaterial({
        color: PALETTE.indigo,
        wireframe: true,
        transparent: true,
        opacity: 0.34,
        depthWrite: false,
      }),
    );
    group.add(shell);

    const core = new THREE.Mesh(new THREE.SphereGeometry(0.72, 32, 32), glowMaterial(PALETTE.cyan, 0.5));
    group.add(core);

    const rings = [
      new THREE.Mesh(new THREE.TorusGeometry(3.2, 0.016, 8, 140), lineMaterial(PALETTE.violet, 0.42)),
      new THREE.Mesh(new THREE.TorusGeometry(3.9, 0.014, 8, 140), lineMaterial(PALETTE.indigo, 0.3)),
      new THREE.Mesh(new THREE.TorusGeometry(2.6, 0.012, 8, 120), lineMaterial(PALETTE.cyan, 0.34)),
    ];
    rings[0].rotation.set(1.15, 0.2, 0.1);
    rings[1].rotation.set(0.55, -0.35, 0.4);
    rings[2].rotation.set(1.5, 0.45, -0.2);
    rings.forEach((ring) => group.add(ring));

    const nodeCount = compact ? 6 : 9;
    const nodeGeo = new THREE.SphereGeometry(0.1, 12, 12);
    const nodes: THREE.Mesh[] = [];
    const beams = new THREE.BufferGeometry();
    const beamPositions = new Float32Array(nodeCount * 6);
    beams.setAttribute('position', new THREE.BufferAttribute(beamPositions, 3));
    const beamLines = new THREE.LineSegments(beams, lineMaterial(PALETTE.cyan, 0.22));
    group.add(beamLines);

    for (let index = 0; index < nodeCount; index += 1) {
      const node = new THREE.Mesh(nodeGeo, glowMaterial(index % 2 ? PALETTE.violet : PALETTE.cyan, 0.7));
      node.userData.phase = (index / nodeCount) * Math.PI * 2;
      node.userData.scale = 1;
      nodes.push(node);
      group.add(node);
    }

    const dustCount = compact ? 60 : 150;
    const dustPositions = new Float32Array(dustCount * 3);
    for (let index = 0; index < dustCount; index += 1) {
      dustPositions[index * 3] = (Math.random() - 0.5) * 26;
      dustPositions[index * 3 + 1] = (Math.random() - 0.5) * 16;
      dustPositions[index * 3 + 2] = (Math.random() - 0.5) * 14;
    }
    const dustGeo = new THREE.BufferGeometry();
    dustGeo.setAttribute('position', new THREE.BufferAttribute(dustPositions, 3));
    scene.add(
      new THREE.Points(
        dustGeo,
        new THREE.PointsMaterial({
          size: 0.05,
          color: PALETTE.indigo,
          transparent: true,
          opacity: 0.24,
          depthWrite: false,
          sizeAttenuation: true,
        }),
      ),
    );

    type Ripple = { mesh: THREE.Mesh; material: THREE.MeshBasicMaterial; life: number };
    const ripples: Ripple[] = [];
    const pointer = { x: 0, y: 0, active: false };
    const ray = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
    const hit = new THREE.Vector3();

    const onMove = (event: PointerEvent) => {
      const rect = container.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const px = (event.clientX - rect.left) / rect.width;
      const py = (event.clientY - rect.top) / rect.height;
      pointer.x = (px - 0.5) * 2;
      pointer.y = (py - 0.5) * 2;
      pointer.active = true;
      ndc.set(px * 2 - 1, -(py * 2 - 1));
      ray.setFromCamera(ndc, camera);
      let hovered = false;
      for (const node of nodes) {
        const distance = ray.ray.distanceToPoint(node.position);
        if (distance < 0.45) hovered = true;
        const material = node.material as THREE.MeshBasicMaterial;
        material.opacity += ((distance < 0.45 ? 1 : 0.7) - material.opacity) * 0.15;
      }
      container.style.cursor = hovered ? 'pointer' : 'crosshair';
    };
    const onLeave = () => {
      pointer.active = false;
      container.style.cursor = 'default';
    };
    const onDown = (event: PointerEvent) => {
      if (reduced || ripples.length >= 3) return;
      const rect = container.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const px = (event.clientX - rect.left) / rect.width;
      const py = (event.clientY - rect.top) / rect.height;
      ndc.set(px * 2 - 1, -(py * 2 - 1));
      ray.setFromCamera(ndc, camera);
      if (!ray.ray.intersectPlane(plane, hit)) return;
      const material = glowMaterial(PALETTE.cyan, 0.8);
      const mesh = new THREE.Mesh(new THREE.TorusGeometry(0.3, 0.016, 8, 64), material);
      const local = group.worldToLocal(hit.clone());
      mesh.position.set(local.x, local.y, local.z + 0.4);
      group.add(mesh);
      ripples.push({ mesh, material, life: 0 });
    };

    container.addEventListener('pointermove', onMove);
    container.addEventListener('pointerleave', onLeave);
    container.addEventListener('pointerdown', onDown);

    let visible = true;
    const observer = new IntersectionObserver(
      (entries) => {
        visible = entries[0]?.isIntersecting ?? true;
      },
      { threshold: 0.05 },
    );
    observer.observe(container);

    const resizeObserver = new ResizeObserver(() => {
      const width = container.clientWidth || 800;
      const height = container.clientHeight || 420;
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, compact ? 1.5 : 2));
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.render(scene, camera);
    });
    resizeObserver.observe(container);

    const clock = new THREE.Clock();
    let frame = 0;
    let running = true;

    const render = () => {
      if (!running) return;
      if (visible && !document.hidden && !reduced) {
        const delta = Math.min(0.06, clock.getDelta());
        const time = clock.elapsedTime;

        camera.position.x += (pointer.x * 2.2 - camera.position.x) * 0.05;
        camera.position.y += (-pointer.y * 1.4 - camera.position.y) * 0.05;
        camera.lookAt(0, 0, 0);

        group.rotation.y = time * 0.1 + pointer.x * 0.35;
        group.rotation.x = pointer.y * 0.18;
        shell.rotation.y = time * 0.22;
        shell.rotation.x = time * 0.12;
        core.scale.setScalar(1 + Math.sin(time * 1.6) * 0.12);
        rings[0].rotation.z = time * 0.18;
        rings[1].rotation.z = -time * 0.12;
        rings[2].rotation.z = time * 0.26;

        nodes.forEach((node, index) => {
          const angle = time * 0.45 + Number(node.userData.phase || 0);
          const radius = 3.2 + (index % 3) * 0.35;
          node.position.set(
            Math.cos(angle) * radius,
            Math.sin(angle * 1.4) * 1.1,
            Math.sin(angle) * radius * 0.45,
          );
          const scale = 1 + Math.sin(time * 1.4 + index) * 0.18;
          node.userData.scale = 1 + (scale - 1) * 0.6;
          node.scale.setScalar(Number(node.userData.scale));

          const base = index * 6;
          beamPositions[base] = 0;
          beamPositions[base + 1] = 0;
          beamPositions[base + 2] = 0;
          beamPositions[base + 3] = node.position.x;
          beamPositions[base + 4] = node.position.y;
          beamPositions[base + 5] = node.position.z;
        });
        (beams.getAttribute('position') as THREE.BufferAttribute).needsUpdate = true;
        (beamLines.material as THREE.LineBasicMaterial).opacity = 0.16 + (Math.sin(time * 1.1) + 1) * 0.1;

        for (let index = ripples.length - 1; index >= 0; index -= 1) {
          const ripple = ripples[index];
          ripple.life += delta;
          const progress = ripple.life / 1;
          ripple.mesh.scale.setScalar(1 + progress * 4);
          ripple.material.opacity = Math.max(0, 0.8 * (1 - progress));
          if (progress >= 1) {
            group.remove(ripple.mesh);
            ripple.mesh.geometry.dispose();
            ripple.material.dispose();
            ripples.splice(index, 1);
          }
        }
      }
      renderer.render(scene, camera);
      frame = window.requestAnimationFrame(render);
    };

    renderer.render(scene, camera);
    if (!reduced) render();

    return () => {
      running = false;
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      resizeObserver.disconnect();
      container.removeEventListener('pointermove', onMove);
      container.removeEventListener('pointerleave', onLeave);
      container.removeEventListener('pointerdown', onDown);
      ripples.forEach(({ mesh, material }) => {
        mesh.geometry.dispose();
        material.dispose();
      });
      disposeStage(stage);
      container.style.cursor = 'default';
  };
}

/* ───────────────────────── 账号权限：权限护盾 ───────────────────────── */

function buildAccessScene(container: HTMLDivElement): () => void {
  const stage = makeStage(container, 45);
    const { scene, camera, renderer, compact } = stage;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    const group = new THREE.Group();
    group.position.set(compact ? 0 : 3.6, 0, 0);
    group.scale.setScalar(compact ? 1.25 : 1.75);
    scene.add(group);

    // 同心六边形：权限层级
    const shield = new THREE.Group();
    group.add(shield);
    [3.5, 2.7, 1.9].forEach((radius, index) => {
      const geometry = new THREE.BufferGeometry().setFromPoints(hexPoints(radius, index * 0.12));
      const loop = new THREE.LineLoop(
        geometry,
        lineMaterial([PALETTE.indigo, PALETTE.violet, PALETTE.cyan][index], 0.5 - index * 0.1),
      );
      shield.add(loop);
    });
    const innerHex = new THREE.Mesh(
      new THREE.CircleGeometry(1.15, 6),
      glowMaterial(PALETTE.teal, 0.14),
    );
    innerHex.rotation.z = Math.PI / 6;
    shield.add(innerHex);

    // 权限格栅：格子依次点亮
    const cols = compact ? 4 : 6;
    const rows = 4;
    const tiles: { mesh: THREE.Mesh; row: number; col: number }[] = [];
    const tileGeo = new THREE.PlaneGeometry(0.42, 0.24);
    for (let row = 0; row < rows; row += 1) {
      for (let col = 0; col < cols; col += 1) {
        const tile = new THREE.Mesh(tileGeo, fillMaterial(PALETTE.indigo, 0.1));
        tile.position.set((col - (cols - 1) / 2) * 0.52, (1.2 - row * 0.8), -0.6);
        group.add(tile);
        tiles.push({ mesh: tile, row, col });
      }
    }

    // 扫描束
    const scan = new THREE.Mesh(new THREE.PlaneGeometry((cols + 1) * 0.52, 0.05), glowMaterial(PALETTE.cyan, 0.45));
    scan.position.set(0, 1.6, -0.4);
    group.add(scan);

    // 环绕的密钥节点
    const nodeCount = compact ? 4 : 6;
    const nodes: THREE.Mesh[] = [];
    for (let index = 0; index < nodeCount; index += 1) {
      const node = new THREE.Mesh(
        new THREE.SphereGeometry(0.085, 12, 12),
        glowMaterial(index % 2 ? PALETTE.violet : PALETTE.teal, 0.72),
      );
      group.add(node);
      nodes.push(node);
    }

    const dustCount = compact ? 40 : 90;
    const dustPositions = new Float32Array(dustCount * 3);
    for (let index = 0; index < dustCount; index += 1) {
      dustPositions[index * 3] = (Math.random() - 0.5) * 24;
      dustPositions[index * 3 + 1] = (Math.random() - 0.5) * 14;
      dustPositions[index * 3 + 2] = (Math.random() - 0.5) * 12;
    }
    const dustGeo = new THREE.BufferGeometry();
    dustGeo.setAttribute('position', new THREE.BufferAttribute(dustPositions, 3));
    scene.add(
      new THREE.Points(
        dustGeo,
        new THREE.PointsMaterial({ size: 0.05, color: PALETTE.violet, transparent: true, opacity: 0.2, depthWrite: false }),
      ),
    );

    const pointer = { x: 0, y: 0, active: false };
    type Ripple = { mesh: THREE.Mesh; material: THREE.MeshBasicMaterial; life: number };
    const ripples: Ripple[] = [];
    const ray = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
    const hit = new THREE.Vector3();

    const onMove = (event: PointerEvent) => {
      const rect = container.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const px = (event.clientX - rect.left) / rect.width;
      const py = (event.clientY - rect.top) / rect.height;
      pointer.x = (px - 0.5) * 2;
      pointer.y = (py - 0.5) * 2;
      pointer.active = true;
      ndc.set(px * 2 - 1, -(py * 2 - 1));
      ray.setFromCamera(ndc, camera);
      container.style.cursor = ray.intersectObjects(tiles.map((item) => item.mesh), false).length ? 'pointer' : 'crosshair';
    };
    const onLeave = () => {
      pointer.active = false;
      container.style.cursor = 'default';
    };
    const onDown = (event: PointerEvent) => {
      if (reduced || ripples.length >= 3) return;
      const rect = container.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const px = (event.clientX - rect.left) / rect.width;
      const py = (event.clientY - rect.top) / rect.height;
      ndc.set(px * 2 - 1, -(py * 2 - 1));
      ray.setFromCamera(ndc, camera);
      if (!ray.ray.intersectPlane(plane, hit)) return;
      const material = glowMaterial(PALETTE.teal, 0.8);
      const mesh = new THREE.Mesh(new THREE.TorusGeometry(0.22, 0.014, 8, 6), material);
      const local = group.worldToLocal(hit.clone());
      mesh.position.set(local.x, local.y, local.z + 0.2);
      group.add(mesh);
      ripples.push({ mesh, material, life: 0 });
    };

    container.addEventListener('pointermove', onMove);
    container.addEventListener('pointerleave', onLeave);
    container.addEventListener('pointerdown', onDown);

    let visible = true;
    const observer = new IntersectionObserver(
      (entries) => {
        visible = entries[0]?.isIntersecting ?? true;
      },
      { threshold: 0.05 },
    );
    observer.observe(container);

    const resizeObserver = new ResizeObserver(() => {
      const width = container.clientWidth || 800;
      const height = container.clientHeight || 420;
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, compact ? 1.5 : 2));
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.render(scene, camera);
    });
    resizeObserver.observe(container);

    const clock = new THREE.Clock();
    let frame = 0;
    let running = true;

    const render = () => {
      if (!running) return;
      if (visible && !document.hidden && !reduced) {
        const delta = Math.min(0.06, clock.getDelta());
        const time = clock.elapsedTime;

        group.rotation.y += ((-0.28 + pointer.x * 0.32) - group.rotation.y) * 0.05;
        group.rotation.x += ((-0.06 - pointer.y * 0.18) - group.rotation.x) * 0.05;
        shield.rotation.z = Math.sin(time * 0.35) * 0.04;

        const sweep = ((time * 0.32) % 1) * 3.4 - 1.7;
        scan.position.y = -1.7 + (sweep + 1.7);
        const scanMaterial = scan.material as THREE.MeshBasicMaterial;
        scanMaterial.opacity = 0.28 + (Math.sin(time * 2) + 1) * 0.12;

        tiles.forEach(({ mesh, row, col }) => {
          const wave = (Math.sin(time * 1.4 - col * 0.35 - row * 0.6) + 1) / 2;
          const nearScan = Math.max(0, 1 - Math.abs(mesh.position.y - scan.position.y) / 0.6);
          const material = mesh.material as THREE.MeshBasicMaterial;
          material.color.setHex(nearScan > 0.4 ? PALETTE.cyan : PALETTE.indigo);
          material.opacity = 0.08 + wave * 0.16 + nearScan * 0.34;
        });

        nodes.forEach((node, index) => {
          const angle = time * 0.4 + (index / nodes.length) * Math.PI * 2;
          node.position.set(Math.cos(angle) * 3.7, Math.sin(angle) * 3.2, Math.sin(angle * 2) * 0.5);
          node.scale.setScalar(1 + Math.sin(time * 1.5 + index) * 0.2);
        });

        for (let index = ripples.length - 1; index >= 0; index -= 1) {
          const ripple = ripples[index];
          ripple.life += delta;
          const progress = ripple.life / 1;
          ripple.mesh.scale.setScalar(1 + progress * 4.2);
          ripple.material.opacity = Math.max(0, 0.75 * (1 - progress));
          if (progress >= 1) {
            group.remove(ripple.mesh);
            ripple.mesh.geometry.dispose();
            ripple.material.dispose();
            ripples.splice(index, 1);
          }
        }
      }
      renderer.render(scene, camera);
      frame = window.requestAnimationFrame(render);
    };

    renderer.render(scene, camera);
    if (!reduced) render();

    return () => {
      running = false;
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      resizeObserver.disconnect();
      container.removeEventListener('pointermove', onMove);
      container.removeEventListener('pointerleave', onLeave);
      container.removeEventListener('pointerdown', onDown);
      ripples.forEach(({ mesh, material }) => {
        mesh.geometry.dispose();
        material.dispose();
      });
      disposeStage(stage);
      container.style.cursor = 'default';
  };
}

export function AmbientScene({ variant }: { variant: 'model' | 'access' }) {
  const host = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const container = host.current;
    if (!container) return;
    return variant === 'model' ? buildModelScene(container) : buildAccessScene(container);
  }, [variant]);
  return <div className={`ambient-scene ambient-${variant}`} ref={host} aria-hidden="true" />;
}

/* ───────────────────────── 项目详情：标书工作面 ───────────────────────── */

export function WorkspaceScene({ step }: { step: string }) {
  const host = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = host.current;
    if (!container) return;
    const stage = makeStage(container, 40);
    const { scene, camera, renderer, compact } = stage;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const accent = STEP_ACCENT[step] || PALETTE.indigo;

    const group = new THREE.Group();
    group.position.set(compact ? 0 : 2.6, 0, 0);
    group.scale.setScalar(compact ? 0.95 : 1.2);
    scene.add(group);

    // 纸张
    const sheetGeo = new THREE.PlaneGeometry(6.2, 3.5);
    const sheet = new THREE.Mesh(sheetGeo, fillMaterial(PALETTE.paper, 0.34));
    group.add(sheet);
    group.add(new THREE.LineSegments(new THREE.EdgesGeometry(sheetGeo), lineMaterial(accent, 0.5)));

    // 章节块：两列
    const blocks: { mesh: THREE.Mesh; edge: THREE.LineSegments; index: number }[] = [];
    const blockGeo = new THREE.PlaneGeometry(1.9, 0.3);
    for (let index = 0; index < 12; index += 1) {
      const row = Math.floor(index / 2);
      const col = index % 2;
      const mesh = new THREE.Mesh(blockGeo, fillMaterial(accent, 0.12));
      const edge = new THREE.LineSegments(new THREE.EdgesGeometry(blockGeo), lineMaterial(accent, 0.32));
      const x = (col - 0.5) * 2.15;
      const y = 1.28 - row * 0.5;
      mesh.position.set(x, y, 0.02);
      edge.position.set(x, y, 0.03);
      group.add(mesh);
      group.add(edge);
      blocks.push({ mesh, edge, index });
    }

    // 步骤专属动效
    const radar = new THREE.Group();
    radar.position.set(0, 0, 0.12);
    const radarRing = new THREE.Mesh(new THREE.TorusGeometry(1.5, 0.03, 8, 100), lineMaterial(PALETTE.cyan, 0.5));
    radar.add(radarRing);
    const radarRing2 = new THREE.Mesh(new THREE.TorusGeometry(0.9, 0.02, 8, 80), lineMaterial(PALETTE.violet, 0.42));
    radar.add(radarRing2);
    const sweep = new THREE.Mesh(new THREE.PlaneGeometry(1.5, 0.02), glowMaterial(PALETTE.cyan, 0.5));
    sweep.position.set(0.75, 0, 0);
    const sweepPivot = new THREE.Group();
    sweepPivot.add(sweep);
    radar.add(sweepPivot);
    radar.visible = step === 'analysis';
    group.add(radar);

    const scanBar = new THREE.Mesh(new THREE.PlaneGeometry(6.0, 0.06), glowMaterial(accent, 0.36));
    scanBar.position.set(0, 1.7, 0.1);
    scanBar.visible = step === 'review';
    group.add(scanBar);

    const seal = new THREE.Group();
    seal.position.set(2.3, -1.2, 0.12);
    const sealRing = new THREE.Mesh(new THREE.TorusGeometry(0.34, 0.024, 8, 60), lineMaterial(PALETTE.teal, 0.7));
    seal.add(sealRing);
    const sealGlow = new THREE.Mesh(new THREE.TorusGeometry(0.4, 0.018, 8, 72), glowMaterial(PALETTE.teal, 0.2));
    seal.add(sealGlow);
    const check = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-0.14, 0.02, 0.04),
      new THREE.Vector3(0.02, -0.16, 0.04),
      new THREE.Vector3(0.24, 0.16, 0.04),
    ]);
    seal.add(new THREE.Line(check, lineMaterial(PALETTE.teal, 0.9)));
    seal.visible = step === 'delivery';
    group.add(seal);

    const progressBar = new THREE.Mesh(new THREE.PlaneGeometry(4.6, 0.06), glowMaterial(PALETTE.violet, 0.4));
    progressBar.position.set(0, -1.62, 0.06);
    progressBar.visible = step === 'writing';
    group.add(progressBar);

    // 浮动粒子
    const dustCount = compact ? 24 : 56;
    const dustPositions = new Float32Array(dustCount * 3);
    for (let index = 0; index < dustCount; index += 1) {
      dustPositions[index * 3] = (Math.random() - 0.5) * 16;
      dustPositions[index * 3 + 1] = (Math.random() - 0.5) * 8;
      dustPositions[index * 3 + 2] = (Math.random() - 0.5) * 8;
    }
    const dustGeo = new THREE.BufferGeometry();
    dustGeo.setAttribute('position', new THREE.BufferAttribute(dustPositions, 3));
    scene.add(
      new THREE.Points(
        dustGeo,
        new THREE.PointsMaterial({ size: 0.05, color: accent, transparent: true, opacity: 0.2, depthWrite: false }),
      ),
    );

    const pointer = { x: 0, y: 0 };
    const onMove = (event: PointerEvent) => {
      const rect = container.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      pointer.x = ((event.clientX - rect.left) / rect.width - 0.5) * 2;
      pointer.y = ((event.clientY - rect.top) / rect.height - 0.5) * 2;
    };
    container.addEventListener('pointermove', onMove);

    let visible = true;
    const observer = new IntersectionObserver(
      (entries) => {
        visible = entries[0]?.isIntersecting ?? true;
      },
      { threshold: 0.05 },
    );
    observer.observe(container);

    const resizeObserver = new ResizeObserver(() => {
      const width = container.clientWidth || 800;
      const height = container.clientHeight || 360;
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, compact ? 1.5 : 2));
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.render(scene, camera);
    });
    resizeObserver.observe(container);

    const clock = new THREE.Clock();
    let frame = 0;
    let running = true;

    const render = () => {
      if (!running) return;
      if (visible && !document.hidden && !reduced) {
        clock.getDelta();
        const time = clock.elapsedTime;

        camera.position.x += (pointer.x * 1.6 + 0.4 - camera.position.x) * 0.05;
        camera.position.y += (-pointer.y * 1.1 + 0.2 - camera.position.y) * 0.05;
        camera.position.z = 12.5;
        camera.lookAt(0, 0, 0);

        group.rotation.y = -0.16 + Math.sin(time * 0.25) * 0.06 + pointer.x * 0.12;
        group.rotation.x = -0.04 - pointer.y * 0.06;

        blocks.forEach(({ mesh, edge, index }) => {
          const material = mesh.material as THREE.MeshBasicMaterial;
          const edgeMaterial = edge.material as THREE.LineBasicMaterial;
          if (step === 'writing') {
            const wave = (Math.sin(time * 1.1 - index * 0.4) + 1) / 2;
            const filled = index < 8 ? 1 : wave;
            material.opacity = 0.08 + filled * 0.32;
            edgeMaterial.opacity = 0.22 + filled * 0.3;
          } else if (step === 'review') {
            const near = Math.max(0, 1 - Math.abs(mesh.position.y - scanBar.position.y) / 0.55);
            material.opacity = 0.08 + near * 0.34;
            edgeMaterial.opacity = 0.24 + near * 0.4;
          } else if (step === 'delivery') {
            material.opacity = 0.3;
            edgeMaterial.opacity = 0.5;
          } else {
            material.opacity = 0.1 + ((Math.sin(time * 1.2 + index * 0.5) + 1) / 2) * 0.14;
            edgeMaterial.opacity = 0.3;
          }
        });

        if (step === 'analysis') {
          sweepPivot.rotation.z = -time * 1.5;
          radarRing.rotation.z = time * 0.2;
          radarRing2.rotation.z = -time * 0.3;
        }
        if (step === 'review') {
          scanBar.position.y = 1.6 - ((time * 0.5) % 1) * 3.2;
        }
        if (step === 'writing') {
          const progress = (Math.sin(time * 0.6) + 1) / 2;
          progressBar.scale.x = 0.2 + progress * 0.8;
          progressBar.position.x = -2.3 + (progressBar.scale.x * 4.6) / 2;
        }
        if (step === 'delivery') {
          seal.scale.setScalar(1 + Math.sin(time * 1.6) * 0.06);
          sealRing.rotation.z = time * 0.25;
          (sealGlow.material as THREE.MeshBasicMaterial).opacity = 0.14 + (Math.sin(time * 1.6) + 1) * 0.1;
        }
        (dustGeo.getAttribute('position') as THREE.BufferAttribute).needsUpdate = false;
      }
      renderer.render(scene, camera);
      frame = window.requestAnimationFrame(render);
    };

    renderer.render(scene, camera);
    if (!reduced) render();

    return () => {
      running = false;
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      resizeObserver.disconnect();
      container.removeEventListener('pointermove', onMove);
      disposeStage(stage);
    };
  }, [step]);

  return <div className="pipeline-scene workspace-scene" ref={host} aria-hidden="true" />;
}
